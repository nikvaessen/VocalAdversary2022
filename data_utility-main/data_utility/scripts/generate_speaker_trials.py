#! /usr/bin/env python3
########################################################################################
#
# Script for generating a trial list based on all unique keys in one or more shards.
#
# Author(s): Nik Vaessen
########################################################################################

import pathlib
import itertools

from collections import defaultdict, OrderedDict
from typing import Tuple, List, Dict, Set, Optional, Iterator, Any, FrozenSet

import click

from tqdm import tqdm
from torch.utils.data import DataLoader

from data_utility.eval.speaker.evaluator import SpeakerTrial
from data_utility.pipe.primitives.shard import load_audio_samples_from_shards
from data_utility.pipe.containers import WavAudioDataSample
from data_utility.util.various import sort_speaker_id_key


########################################################################################
# Aggregate all unique samples


class WavAudioDataSampleTrialAggregator:
    def __init__(self):
        self.speaker_ids = set()
        self.sample_ids = set()

        self.map_sample_id_to_speaker_id = dict()
        self._map_speaker_id_to_gender = defaultdict(set)

    def __call__(self, x: WavAudioDataSample):
        assert isinstance(x, WavAudioDataSample)

        sample_id = x.key

        if sample_id in self.sample_ids:
            raise ValueError(f"non-unique {sample_id=}")
        if x.speaker_id is None:
            raise ValueError(f"{sample_id=} is missing speaker_id label")
        if x.gender is None:
            raise ValueError(f"{sample_id=} is missing gender label")

        speaker_id = x.speaker_id
        gender = x.gender

        self.sample_ids.add(sample_id)
        self.speaker_ids.add(speaker_id)

        self.map_sample_id_to_speaker_id[sample_id] = speaker_id
        self._map_speaker_id_to_gender[speaker_id].add(gender)

    def get_gender_mapping(self):
        map_to_return = OrderedDict()

        for speaker_id in sorted(
            self._map_speaker_id_to_gender.keys(), key=sort_speaker_id_key
        ):
            gender_set = self._map_speaker_id_to_gender[speaker_id]

            if len(gender_set) != 1:
                raise ValueError(f"{speaker_id=} has gender map {gender_set}")

            gender = gender_set.pop()
            assert gender == "f" or gender == "m"
            map_to_return[speaker_id] = gender

        return map_to_return


########################################################################################
# Create a trial list based on the aggregated data


def generate_speaker_trials(
    samples: Set[str],
    map_sample_to_speaker: Dict[str, str],
    map_speaker_to_gender: Dict[str, str],
    ensure_same_sex_trials: bool = True,
    limit_num_trials: Optional[int] = None,
) -> List[SpeakerTrial]:
    samples_per_speaker = _samples_per_speaker(samples, map_sample_to_speaker)

    # generale all positive pairs until exhaustion or until `limit_num_trials` is met
    print("generating positive pairs")
    pos_pairs = set(
        SpeakerTrial(left, right, same_speaker=True)
        for left, right in _positive_pairs(samples_per_speaker, limit_num_trials)
    )

    # generale all negative pairs until exhaustion or until `limit_num_trials` is met
    print("generating negative pairs")
    neg_pairs = set(
        SpeakerTrial(left, right, same_speaker=False)
        for left, right in _negative_pairs(
            samples_per_speaker,
            ensure_same_sex_trials,
            map_speaker_to_gender,
            limit_num_trials,
        )
    )

    # return as list
    pos_pairs = sorted(list(pos_pairs), key=lambda x: str(x))
    neg_pairs = sorted(list(neg_pairs), key=lambda x: str(x))

    return pos_pairs + neg_pairs


def _exhaust_pairs_iterator_dictionary(
    generators: Dict[Any, Iterator[Tuple[str, str]]],
    limit_num_trials: Optional[int] = None,
) -> List[Tuple[str, str]]:
    # loop over each generator, popping one pair, until all are exhausted
    # or we have reached `lim_num_trials`
    pairs: Set[FrozenSet] = set()
    key_queue = sorted(generators.keys())

    def continue_loop():
        if limit_num_trials is None:
            return len(generators) > 0
        else:
            return len(generators) > 0 and len(pairs) < limit_num_trials

    with tqdm() as p:
        while continue_loop():
            p.update(1)

            if len(key_queue) == 0:
                key_queue = sorted(generators.keys())

            key = key_queue.pop()
            generator = generators[key]

            pair = next(generator, None)

            if pair is None:
                # remove from dictionary as it is exhausted
                del generators[key]
                continue

            assert len(pair) == 2

            p1, p2 = pair
            pair = frozenset([p1, p2])

            if pair in pairs:
                raise ValueError("duplicate entry")
            else:
                pairs.add(pair)

    return [tuple(p) for p in pairs]


def _positive_pairs(
    samples_per_speaker: Dict[str, List[str]], limit_num_trials: Optional[int] = None
) -> List[Tuple[str, str]]:
    # for each speaker, create a generator of pairs from samples of the same speaker
    generators = {}

    for k, v in sorted(samples_per_speaker.items()):
        generators[k] = itertools.combinations(v, r=2)

    return _exhaust_pairs_iterator_dictionary(generators, limit_num_trials)


def _negative_pairs(
    samples_per_speaker: Dict[str, List[str]],
    ensure_same_sex_trials: bool,
    gender_map: Optional[Dict[str, str]],
    limit_num_trials: Optional[int] = None,
) -> List[Tuple[str, str]]:
    # find each speaker combination
    speaker_pair_set = set()

    for k1 in sorted(samples_per_speaker.keys()):
        for k2 in sorted(samples_per_speaker.keys()):
            if k1 == k2 or (
                ensure_same_sex_trials and gender_map[k1] != gender_map[k2]
            ):
                continue

            speaker_pair_set.add(tuple(sorted([k1, k2])))

    # for each speaker combination, create a generator
    generators = {}

    for speaker_combination in speaker_pair_set:
        k1, k2 = speaker_combination

        v1 = samples_per_speaker[k1]
        v2 = samples_per_speaker[k2]

        generators[speaker_combination] = itertools.product(v1, v2)

    return _exhaust_pairs_iterator_dictionary(generators, limit_num_trials)


def _samples_per_speaker(
    samples: Set[str],
    map_sample_to_speaker: Dict[str, str],
) -> Dict[str, List[str]]:
    samples_per_speaker = defaultdict(list)

    for sample in samples:
        speaker = map_sample_to_speaker[sample]
        samples_per_speaker[speaker].append(sample)

    return samples_per_speaker


########################################################################################
# Entrypoint of CLI


@click.command()
@click.argument(
    "dirs",
    nargs=-1,
    type=pathlib.Path,
    required=True,
)
@click.option(
    "--out",
    "save_path",
    type=pathlib.Path,
    required=True,
    help="path to write trials to",
)
@click.option(
    "--same-sex",
    "ensure_same_sex_trials",
    type=bool,
    default=True,
    help="if set, negative trials will involve only speakers with the same gender",
)
@click.option(
    "--limit",
    "limit_num_trials",
    type=int,
    default=None,
    required=False,
    help="if set, limit number of positive and negative trials to given number",
)
def main(
    dirs: Tuple[pathlib.Path],
    save_path: pathlib.Path,
    ensure_same_sex_trials: bool,
    limit_num_trials: int,
):
    # create data pipeline
    dp = load_audio_samples_from_shards(
        list(dirs), allow_partial=True, shuffle_buffer=1
    )

    # loop over all samples in pipeline
    ag = WavAudioDataSampleTrialAggregator()
    for x in tqdm(DataLoader(dp, batch_size=None, num_workers=0)):
        ag(x)

    # collect data
    samples = ag.sample_ids
    gender_map = ag.get_gender_mapping()
    sample_map = ag.map_sample_id_to_speaker_id

    # generate trials
    trials = generate_speaker_trials(
        samples=samples,
        map_sample_to_speaker=sample_map,
        map_speaker_to_gender=gender_map,
        ensure_same_sex_trials=ensure_same_sex_trials,
        limit_num_trials=limit_num_trials,
    )

    # write trials to file
    SpeakerTrial.to_file(save_path, trials)


if __name__ == "__main__":
    main()
