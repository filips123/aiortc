import math
from enum import Enum

from aiortc.utils import uint32_add, uint32_gt

BURST_DELTA_THRESHOLD_MS = 5

# overuse detector
MAX_ADAPT_OFFSET_MS = 15
MIN_NUM_DELTAS = 60

# overuse estimator
DELTA_COUNTER_MAX = 1000
MIN_FRAME_PERIOD_HISTORY_LENGTH = 60

# abs-send-time estimator
INTER_ARRIVAL_SHIFT = 26
TIMESTAMP_GROUP_LENGTH_MS = 5
TIMESTAMP_TO_MS = 1000.0 / (1 << INTER_ARRIVAL_SHIFT)


class BandwidthUsage(Enum):
    NORMAL = 0
    UNDERUSING = 1
    OVERUSING = 2


class TimestampGroup:
    def __init__(self, timestamp=None):
        self.arrival_time = None
        self.first_timestamp = timestamp
        self.last_timestamp = timestamp
        self.size = 0


class InterArrivalDelta:
    def __init__(self, timestamp, arrival_time, size):
        self.timestamp = timestamp
        self.arrival_time = arrival_time
        self.size = size


class InterArrival:
    """
    Inter-arrival time and size filter.

    Adapted from the webrtc.org codebase.
    """
    def __init__(self, group_length, timestamp_to_ms):
        self.group_length = group_length
        self.timestamp_to_ms = timestamp_to_ms
        self.current_group = None
        self.previous_group = None

    def compute_deltas(self, timestamp, arrival_time, packet_size):
        deltas = None
        if self.current_group is None:
            self.current_group = TimestampGroup(timestamp)
        elif self.packet_out_of_order(timestamp):
            return deltas
        elif self.new_timestamp_group(timestamp, arrival_time):
            if self.previous_group is not None:
                deltas = InterArrivalDelta(
                    timestamp=uint32_add(self.current_group.last_timestamp,
                                         -self.previous_group.last_timestamp),
                    arrival_time=(self.current_group.arrival_time -
                                  self.previous_group.arrival_time),
                    size=self.current_group.size - self.previous_group.size)

            # shift groups
            self.previous_group = self.current_group
            self.current_group = TimestampGroup(timestamp=timestamp)
        elif uint32_gt(timestamp, self.current_group.last_timestamp):
            self.current_group.last_timestamp = timestamp

        self.current_group.size += packet_size
        self.current_group.arrival_time = arrival_time

        return deltas

    def belongs_to_burst(self, timestamp, arrival_time):
        timestamp_delta = uint32_add(timestamp, -self.current_group.last_timestamp)
        timestamp_delta_ms = round(self.timestamp_to_ms * timestamp_delta)
        arrival_time_delta = arrival_time - self.current_group.arrival_time
        return (timestamp_delta_ms == 0 or
                ((arrival_time_delta - timestamp_delta_ms) < 0 and
                 arrival_time_delta <= BURST_DELTA_THRESHOLD_MS))

    def new_timestamp_group(self, timestamp, arrival_time):
        if self.belongs_to_burst(timestamp, arrival_time):
            return False
        else:
            timestamp_delta = uint32_add(timestamp, -self.current_group.first_timestamp)
            return timestamp_delta > self.group_length

    def packet_out_of_order(self, timestamp):
        timestamp_delta = uint32_add(timestamp, -self.current_group.first_timestamp)
        return timestamp_delta >= 0x80000000


class OveruseDetector:
    """
    Bandwidth overuse detector.

    Adapted from the webrtc.org codebase.
    """
    def __init__(self):
        self.hypothesis = BandwidthUsage.NORMAL
        self.last_update_ms = None
        self.k_up = 0.0087
        self.k_down = 0.039
        self.overuse_counter = 0
        self.overuse_time = None
        self.overuse_time_threshold = 10
        self.previous_offset = 0
        self.threshold = 12.5

    def detect(self, offset, timestamp_delta_ms, num_of_deltas, now_ms):
        if num_of_deltas < 2:
            return BandwidthUsage.NORMAL

        T = min(num_of_deltas, MIN_NUM_DELTAS) * offset
        if T > self.threshold:
            if self.overuse_time is None:
                self.overuse_time = timestamp_delta_ms / 2
            else:
                self.overuse_time += timestamp_delta_ms
            self.overuse_counter += 1

            if (self.overuse_time > self.overuse_time_threshold and
               self.overuse_counter > 1 and
               offset >= self.previous_offset):
                self.overuse_counter = 0
                self.overuse_time = 0
                self.hypothesis = BandwidthUsage.OVERUSING
        elif T < -self.threshold:
            self.overuse_counter = 0
            self.overuse_time = None
            self.hypothesis = BandwidthUsage.UNDERUSING
        else:
            self.overuse_counter = 0
            self.overuse_time = None
            self.hypothesis = BandwidthUsage.NORMAL

        self.previous_offset = offset
        self.update_threshold(T, now_ms)
        return self.hypothesis

    def state(self):
        return self.hypothesis

    def update_threshold(self, modified_offset, now_ms):
        if self.last_update_ms is None:
            self.last_update_ms = now_ms

        if abs(modified_offset) > self.threshold + MAX_ADAPT_OFFSET_MS:
            self.last_update_ms = now_ms
            return

        k = self.k_down if abs(modified_offset) < self.threshold else self.k_up
        time_delta_ms = min(now_ms - self.last_update_ms, 100)
        self.threshold += k * (abs(modified_offset) - self.threshold) * time_delta_ms
        self.threshold = max(6, min(self.threshold, 600))
        self.last_update_ms = now_ms


class OveruseEstimator:
    """
    Bandwidth overuse estimator.

    Adapted from the webrtc.org codebase.
    """
    def __init__(self):
        self.E = [
            [100, 0],
            [0, 0.1],
        ]
        self._num_of_deltas = 0
        self._offset = 0
        self.previous_offset = 0
        self.slope = 1 / 64
        self.ts_delta_hist = []

        self.avg_noise = 0
        self.var_noise = 50
        self.process_noise = [
            1e-13,
            1e-3
        ]

    def num_of_deltas(self):
        return self._num_of_deltas

    def offset(self):
        return self._offset

    def update(self, time_delta_ms, timestamp_delta_ms, size_delta,
               current_hypothesis, now_ms):
        min_frame_period = self.update_min_frame_period(timestamp_delta_ms)
        t_ts_delta = time_delta_ms - timestamp_delta_ms
        fs_delta = size_delta

        self._num_of_deltas = min(self._num_of_deltas + 1, DELTA_COUNTER_MAX)

        # update  Kalman filter
        self.E[0][0] += self.process_noise[0]
        self.E[1][1] += self.process_noise[1]
        if ((current_hypothesis == BandwidthUsage.OVERUSING and
           self._offset < self.previous_offset) or
           (current_hypothesis == BandwidthUsage.UNDERUSING and
           self._offset > self.previous_offset)):
            self.E[1][1] += 10 * self.process_noise[1]

        h = [fs_delta, 1.0]
        Eh = [
            self.E[0][0] * h[0] + self.E[0][1] * h[1],
            self.E[1][0] * h[0] + self.E[1][1] * h[1]
        ]

        # update noise estimate
        residual = t_ts_delta - self.slope * h[0] - self._offset
        if current_hypothesis == BandwidthUsage.NORMAL:
            max_residual = 3.0 * math.sqrt(self.var_noise)
            if abs(residual) < max_residual:
                self.update_noise_estimate(residual, min_frame_period)
            else:
                self.update_noise_estimate(-max_residual if residual < 0 else max_residual,
                                           min_frame_period)

        denom = self.var_noise + h[0] * Eh[0] + h[1] * Eh[1]
        K = [Eh[0] / denom, Eh[1] / denom]

        IKh = [
            [1.0 - K[0] * h[0], -K[0] * h[1]],
            [-K[1] * h[0], 1.0 - K[1] * h[1]]
        ]
        e00 = self.E[0][0]
        e01 = self.E[0][1]

        # update state
        self.E[0][0] = e00 * IKh[0][0] + self.E[1][0] * IKh[0][1]
        self.E[0][1] = e01 * IKh[0][0] + self.E[1][1] * IKh[0][1]
        self.E[1][0] = e00 * IKh[1][0] + self.E[1][0] * IKh[1][1]
        self.E[1][1] = e01 * IKh[1][0] + self.E[1][1] * IKh[1][1]

        self.previous_offset = self._offset
        self.slope += K[0] * residual
        self._offset += K[1] * residual

    def update_min_frame_period(self, ts_delta):
        min_frame_period = ts_delta
        if len(self.ts_delta_hist) >= MIN_FRAME_PERIOD_HISTORY_LENGTH:
            self.ts_delta_hist.pop(0)

        for old_ts_delta in self.ts_delta_hist:
            min_frame_period = min(old_ts_delta, min_frame_period)

        self.ts_delta_hist.append(ts_delta)
        return min_frame_period

    def update_noise_estimate(self, residual, ts_delta):
        alpha = 0.01
        if self._num_of_deltas > 10 * 30:
            alpha = 0.002

        beta = pow(1 - alpha, ts_delta * 30.0 / 1000.0)
        self.avg_noise = beta * self.avg_noise + (1 - beta) * residual
        self.var_noise = beta * self.var_noise + (1 - beta) * (self.avg_noise - residual) ** 2

        if self.var_noise < 1:
            self.var_noise = 1


class RateBucket:
    def __init__(self, count=0, value=0):
        self.count = count
        self.value = value

    def __eq__(self, other):
        return self.count == other.count and self.value == other.value


class RateCounter:
    """
    Rate counter, which stores the amount received in 1ms buckets.
    """
    def __init__(self, window_size, scale=8000):
        self._scale = scale
        self._window_size = window_size
        self.reset()

    def add(self, value, now_ms):
        if self._origin_ms is None:
            self._origin_ms = now_ms
        else:
            self._erase_old(now_ms)

        index = (self._origin_index + now_ms - self._origin_ms) % self._window_size
        self._buckets[index].count += 1
        self._buckets[index].value += value
        self._total.count += 1
        self._total.value += value

    def rate(self, now_ms):
        if self._origin_ms is not None:
            self._erase_old(now_ms)
            active_window_size = now_ms - self._origin_ms + 1
            if self._total.count > 0 and active_window_size > 1:
                return round(self._scale * self._total.value / active_window_size)

    def reset(self):
        self._buckets = [RateBucket() for i in range(self._window_size)]
        self._origin_index = 0
        self._origin_ms = None
        self._total = RateBucket()

    def _erase_old(self, now_ms):
        new_origin_ms = now_ms - self._window_size + 1
        while self._origin_ms < new_origin_ms:
            bucket = self._buckets[self._origin_index]
            self._total.count -= bucket.count
            self._total.value -= bucket.value
            bucket.count = 0
            bucket.value = 0

            self._origin_index = (self._origin_index + 1) % self._window_size
            self._origin_ms += 1


class RemoteBitrateEstimator:
    def __init__(self):
        self.incoming_bitrate = RateCounter(1000, 8000)
        self.incoming_bitrate_initialized = True
        self.inter_arrival = InterArrival(
            (TIMESTAMP_GROUP_LENGTH_MS << INTER_ARRIVAL_SHIFT) // 1000,
            TIMESTAMP_TO_MS)
        self.estimator = OveruseEstimator()
        self.detector = OveruseDetector()
        self.ssrcs = {}

    def add(self, arrival_time_ms, abs_send_time, payload_size, ssrc):
        timestamp = abs_send_time << 8

        # make note of SSRC
        self.ssrcs[ssrc] = arrival_time_ms

        # update incoming bitrate
        if self.incoming_bitrate.rate(arrival_time_ms) is not None:
            self.incoming_bitrate_initialized = True
        elif self.incoming_bitrate_initialized:
            self.incoming_bitrate.reset()
            self.incoming_bitrate_initialized = False
        self.incoming_bitrate.add(payload_size, arrival_time_ms)

        # calculate inter-arrival deltas
        deltas = self.inter_arrival.compute_deltas(timestamp, arrival_time_ms, payload_size)
        if deltas is not None:
            timestamp_delta_ms = int(deltas.timestamp * TIMESTAMP_TO_MS)
            self.estimator.update(
                deltas.arrival_time,
                timestamp_delta_ms,
                deltas.size,
                self.detector.state(),
                arrival_time_ms)
            self.detector.detect(
                self.estimator.offset(),
                timestamp_delta_ms,
                self.estimator.num_of_deltas(),
                arrival_time_ms)