import logging
import random
import sys
import time
from abc import ABC

import apache_beam as beam
import typing

from apache_beam import RestrictionProvider
from apache_beam.io.iobase import RestrictionTracker
from apache_beam.io.restriction_trackers import OffsetRange, OffsetRestrictionTracker
from apache_beam.io.watermark_estimators import WalltimeWatermarkEstimator
from apache_beam.runners.sdf_utils import RestrictionTrackerView


class MyPartition:
    def __init__(self, id: int, last_offset: int, committed_offset: int = 0):
        self.id = id
        self._last_offset = last_offset
        self._committed_offset = committed_offset

    def poll(self) -> typing.Optional[int]:
        offset = self._committed_offset + 1
        if offset > self._last_offset:
            return

        return offset

    def commit(self):
        self._committed_offset += 1

    def size(self) -> int:
        return self._last_offset + 1  # offsets start at 0

    def get_committed_position(self) -> int:
        return self._committed_offset

    def add_new_messages(self, n: int):
        self._last_offset += n


class MyPartitionRestrictionTracker(OffsetRestrictionTracker):
    def try_split(self, fraction_of_remainder):
        if not self._checkpointed:
            if self._last_claim_attempt is None:
                cur = self._range.start - 1
            else:
                cur = self._last_claim_attempt
            split_point = cur + 1  # for partitions Kafka-style
            if split_point <= self._range.stop:
                if fraction_of_remainder == 0:
                    self._checkpointed = True
                self._range, residual_range = self._range.split_at(split_point)
                return self._range, residual_range

    def is_bounded(self):
        return False


class GeneratePartitionsDoFn(beam.DoFn, ABC):
    NUM_PARTITIONS = 4
    INITIAL_MAX_SIZE = 120
    MAX_INITIAL_COMMITTED = 20

    def process(self, ignored_element, *args, **kwargs):
        for k in range(self.NUM_PARTITIONS):
            committed_offset = random.randint(0, self.MAX_INITIAL_COMMITTED)
            yield MyPartition(id=k,
                              last_offset=random.randint(committed_offset, self.INITIAL_MAX_SIZE),
                              committed_offset=committed_offset)


class ProcessPartitionsSplittableDoFn(beam.DoFn, RestrictionProvider, ABC):
    POLL_TIMEOUT = 0.1
    MIN_ADD_NEW_MSGS = 20
    MAX_ADD_NEW_MSGS = 100
    PROB_NEW_MSGS = 0.01
    MAX_EMPTY_POLLS = 10

    @beam.DoFn.unbounded_per_element()
    def process(self,
                element: MyPartition,
                tracker: RestrictionTrackerView = beam.DoFn.RestrictionParam(),
                wm_estim=beam.DoFn.WatermarkEstimatorParam(WalltimeWatermarkEstimator.default_provider()),
                **unused_kwargs) -> typing.Iterable[typing.Tuple[int, str]]:
        n_times_empty = 0
        while True:
            offset_to_process = element.poll()
            if offset_to_process is not None:
                if tracker.try_claim(offset_to_process):
                    msg = f"Processed: {offset_to_process}   Last: {element.size()}"
                    yield element.id, msg
                    element.commit()
                else:
                    return

            # Code to add more messages to simulate real word scenarios
            if offset_to_process is None:
                logging.info(f" ** Partition {element.id}: Empty poll. Waiting")
                n_times_empty += 1

            if n_times_empty > self.MAX_EMPTY_POLLS:
                logging.info(f" ** Partition {element.id}: Waiting for too long. Adding more messages")
                self._add_new_messages(element)
                n_times_empty = 0
            elif random.random() <= self.PROB_NEW_MSGS:
                logging.info(f" ** Partition {element.id}: Bingo! Adding more messages")
                self._add_new_messages(element)

            time.sleep(self.POLL_TIMEOUT)

    def _add_new_messages(self, element: MyPartition):
        element.add_new_messages(random.randint(self.MIN_ADD_NEW_MSGS, self.MAX_ADD_NEW_MSGS))

    def create_tracker(self, restriction: OffsetRange) -> RestrictionTracker:
        return MyPartitionRestrictionTracker(restriction)

    def initial_restriction(self, element: MyPartition) -> OffsetRange:
        committed_offset = element.get_committed_position()
        if committed_offset is None:
            committed_offset = -1
        return OffsetRange(committed_offset + 1, sys.maxsize)

    def restriction_size(self, element: MyPartition, restriction: OffsetRange):
        return restriction.size()
