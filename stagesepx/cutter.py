import os
import typing
import random
import cv2
import uuid
import numpy as np
from loguru import logger
from findit import FindIt

from stagesepx import toolbox


class VideoCutRange(object):
    def __init__(self,
                 video_path: str,
                 start: int,
                 end: int,
                 ssim: typing.List,
                 start_time: float,
                 end_time: float):
        self.video_path = video_path
        self.start = start
        self.end = end
        self.ssim = ssim
        self.start_time = start_time
        self.end_time = end_time

        # if length is 1
        # https://github.com/williamfzc/stagesepx/issues/9
        if start > end:
            self.start, self.end = self.end, self.start
            self.start_time, self.end_time = self.end_time, self.start_time

    def can_merge(self, another: 'VideoCutRange', offset: int = None, **_):
        if not offset:
            is_continuous = self.end == another.start
        else:
            is_continuous = self.end + offset >= another.start
        return is_continuous and self.video_path == another.video_path

    def merge(self, another: 'VideoCutRange', **kwargs) -> 'VideoCutRange':
        assert self.can_merge(another, **kwargs)
        return __class__(
            self.video_path,
            self.start,
            another.end,
            self.ssim + another.ssim,
            self.start_time,
            another.end_time,
        )

    def contain(self, frame_id: int) -> bool:
        return frame_id in range(self.start, self.end + 1)

    def contain_image(self,
                      image_path: str = None,
                      image_object: np.ndarray = None,
                      threshold: float = None,
                      *args, **kwargs):
        assert image_path or image_object, 'should fill image_path or image_object'
        if not threshold:
            threshold = 0.99

        if image_path:
            logger.debug(f'found image path, use it first: {image_path}')
            assert os.path.isfile(image_path), f'image {image_path} not existed'
            image_object = cv2.imread(image_path)
        image_object = toolbox.turn_grey(image_object)

        # TODO use client or itself..?
        fi = FindIt(
            engine=['template']
        )
        fi_template_name = 'default'
        fi.load_template(fi_template_name, pic_object=image_object)

        with toolbox.video_capture(self.video_path) as cap:
            target_id = self.pick(*args, **kwargs)[0]
            frame = toolbox.get_frame(cap, target_id)
            frame = toolbox.turn_grey(frame)

            result = fi.find(str(target_id), target_pic_object=frame)
        find_result = result['data'][fi_template_name]['TemplateEngine']
        position = find_result['target_point']
        sim = find_result['target_sim']
        logger.debug(f'position: {position}, sim: {sim}')
        return sim > threshold

    def pick(self,
             frame_count: int = None,
             is_random: bool = None,
             *_, **__) -> typing.List[int]:
        if not frame_count:
            frame_count = 1

        result = list()
        if is_random:
            return random.sample(range(self.start, self.end), frame_count)
        length = self.get_length()
        for _ in range(frame_count):
            cur = int(self.start + length / frame_count * _)
            result.append(cur)
        return result

    def get_length(self):
        return self.end - self.start + 1

    def is_stable(self, threshold: float = None, **_) -> bool:
        if not threshold:
            threshold = 0.95
        return np.mean(self.ssim) > threshold

    def is_loop(self, threshold: float = None, **_) -> bool:
        if not threshold:
            threshold = 0.95
        with toolbox.video_capture(video_path=self.video_path) as cap:
            start_frame = toolbox.get_frame(cap, self.start)
            end_frame = toolbox.get_frame(cap, self.end)
            start_frame, end_frame = map(toolbox.compress_frame, (start_frame, end_frame))
            return toolbox.compare_ssim(start_frame, end_frame) > threshold

    def __str__(self):
        return f'<VideoCutRange [{self.start}-{self.end}] ssim={self.ssim}>'

    __repr__ = __str__


class VideoCutResult(object):
    def __init__(self,
                 video_path: str,
                 ssim_list: typing.List[VideoCutRange]):
        self.video_path = video_path
        self.ssim_list = ssim_list

    def get_target_range_by_id(self, frame_id: int) -> VideoCutRange:
        for each in self.ssim_list:
            if each.contain(frame_id):
                return each
        raise RuntimeError(f'frame {frame_id} not found in video')

    @staticmethod
    def _length_filter(range_list: typing.List[VideoCutRange], limit: int) -> typing.List[VideoCutRange]:
        after = list()
        for each in range_list:
            if each.get_length() >= limit:
                after.append(each)
        return after

    def get_unstable_range(self,
                           limit: int = None,
                           range_threshold: float = None,
                           **kwargs) -> typing.List[VideoCutRange]:
        """ return unstable range only """
        change_range_list = sorted(
            [i for i in self.ssim_list if not i.is_stable(**kwargs)],
            key=lambda x: x.start)

        # merge
        i = 0
        merged_change_range_list = list()
        while i < len(change_range_list) - 1:
            cur = change_range_list[i]
            while cur.can_merge(change_range_list[i + 1], **kwargs):
                # can be merged
                i += 1
                cur = cur.merge(change_range_list[i], **kwargs)

                # out of range
                if i + 1 >= len(change_range_list):
                    break
            merged_change_range_list.append(cur)
            i += 1
        if change_range_list[-1].start > merged_change_range_list[-1].end:
            merged_change_range_list.append(change_range_list[-1])

        if limit:
            merged_change_range_list = self._length_filter(merged_change_range_list, limit)
        # merged range check
        if range_threshold:
            merged_change_range_list = [i for i in merged_change_range_list if not i.is_loop(range_threshold)]
        logger.debug(f'unstable range of [{self.video_path}]: {merged_change_range_list}')
        return merged_change_range_list

    def get_range(self,
                  limit: int = None,
                  **kwargs) -> typing.Tuple[typing.List[VideoCutRange], typing.List[VideoCutRange]]:
        """
        return stable_range_list and unstable_range_list

        :param limit: ignore some ranges which are too short, 5 means ignore unstable ranges which length < 5
        :param kwargs:
            threshold: float, 0-1, default to 0.95. decided whether a range is stable. larger => more unstable ranges
            range_threshold:
                same as threshold, but it decided whether a merged range is stable.
                see https://github.com/williamfzc/stagesepx/issues/17 for details
            offset:
                it will change the way to decided whether two ranges can be merged
                before: first_range.end == second_range.start
                after: first_range.end + offset >= secord_range.start
        :return:
        """
        unstable_range_list = self.get_unstable_range(limit, **kwargs)

        # it is not a real frame (not existed)
        # just take it as a beginning
        # real frame id is started with 1, with non-zero timestamp
        video_start_frame_id = 0
        video_start_timestamp = 0.

        video_end_frame_id = self.ssim_list[-1].end
        video_end_timestamp = self.ssim_list[-1].end_time

        first_stable_range_end_id = unstable_range_list[0].start - 1
        end_stable_range_start_id = unstable_range_list[-1].end

        # IMPORTANT: len(ssim_list) + 1 == video_end_frame_id
        range_list = [
            # first stable range
            VideoCutRange(
                self.video_path,
                video_start_frame_id,
                first_stable_range_end_id,
                [1.],
                video_start_timestamp,
                self.get_target_range_by_id(first_stable_range_end_id - 1).start_time,
            ),
            # last stable range
            VideoCutRange(
                self.video_path,
                end_stable_range_start_id,
                video_end_frame_id,
                [1.],
                self.get_target_range_by_id(end_stable_range_start_id - 1).end_time,
                video_end_timestamp,
            ),
        ]
        # diff range
        for i in range(len(unstable_range_list) - 1):
            range_start_id = unstable_range_list[i].end + 1
            range_end_id = unstable_range_list[i + 1].start - 1
            range_list.append(
                VideoCutRange(
                    self.video_path,
                    range_start_id,
                    range_end_id,
                    [1.],
                    self.get_target_range_by_id(range_start_id - 1).start_time,
                    self.get_target_range_by_id(range_end_id - 1).end_time,
                )
            )

        # remove some ranges, which is limit
        if limit:
            range_list = self._length_filter(range_list, limit)
        logger.debug(f'stable range of [{self.video_path}]: {range_list}')
        stable_range_list = sorted(range_list, key=lambda x: x.start)
        return stable_range_list, unstable_range_list

    def get_stable_range(self, limit: int = None, **kwargs) -> typing.List[VideoCutRange]:
        """ return stable range only """
        return self.get_range(limit, **kwargs)[0]

    def thumbnail(self,
                  target_range: VideoCutRange,
                  to_dir: str = None,
                  compress_rate: float = None,
                  is_vertical: bool = None) -> np.ndarray:
        """
        build a thumbnail, for easier debug or something else

        :param target_range: VideoCutRange
        :param to_dir: your thumbnail will be saved to this path
        :param compress_rate: float, 0 - 1, about thumbnail's size, default to 0.1 (1/10)
        :param is_vertical: direction
        :return:
        """
        if not compress_rate:
            compress_rate = 0.1
        # direction
        if is_vertical:
            stack_func = np.vstack
        else:
            stack_func = np.hstack

        frame_list = list()
        with toolbox.video_capture(self.video_path) as cap:
            toolbox.video_jump(cap, target_range.start)
            ret, frame = cap.read()
            count = 1
            length = target_range.get_length()
            while ret and count <= length:
                frame = toolbox.compress_frame(frame, compress_rate)
                frame_list.append(frame)
                ret, frame = cap.read()
                count += 1
        merged = stack_func(frame_list)

        # create parent dir
        if to_dir:
            target_path = os.path.join(to_dir, f'thumbnail_{target_range.start}-{target_range.end}.png')
            cv2.imwrite(target_path, merged)
            logger.debug(f'save thumbnail to {target_path}')
        return merged

    def pick_and_save(self,
                      range_list: typing.List[VideoCutRange],
                      frame_count: int,
                      to_dir: str = None,

                      # in kwargs
                      # compress_rate: float = None,
                      # target_size: typing.Tuple[int, int] = None,
                      # to_grey: bool = None,

                      *args, **kwargs) -> str:
        """
        pick some frames from range, and save them as files

        :param range_list: VideoCutRange list
        :param frame_count: default to 3, and finally you will get 3 frames for each range
        :param to_dir: will saved to this path
        :param args:
        :param kwargs:
        :return:
        """
        stage_list = list()
        for index, each_range in enumerate(range_list):
            picked = each_range.pick(frame_count, *args, **kwargs)
            logger.info(f'pick {picked} in range {each_range}')
            stage_list.append((index, picked))

        # create parent dir
        if not to_dir:
            to_dir = toolbox.get_timestamp_str()
        os.makedirs(to_dir, exist_ok=True)

        for each_stage_id, each_frame_list in stage_list:
            # create sub dir
            each_stage_dir = os.path.join(to_dir, str(each_stage_id))
            os.makedirs(each_stage_dir, exist_ok=True)

            with toolbox.video_capture(self.video_path) as cap:
                for each_frame_id in each_frame_list:
                    each_frame_path = os.path.join(each_stage_dir, f'{uuid.uuid4()}.png')
                    each_frame = toolbox.get_frame(cap, each_frame_id - 1)
                    each_frame = toolbox.compress_frame(each_frame, **kwargs)
                    cv2.imwrite(each_frame_path, each_frame)
                    logger.debug(f'frame [{each_frame_id}] saved to {each_frame_path}')

        return to_dir


class VideoCutter(object):
    def __init__(self,
                 step: int = None,
                 # TODO removed in the future
                 compress_rate: float = None):
        """
        init video cutter

        :param step: step between frames, default to 1
        :param compress_rate: (moved to `cut`) before * compress_rate = after
        """
        if not step:
            step = 1
        self.step = step

        if compress_rate:
            logger.warning('compress_rate has been moved to func `cut`')

    @staticmethod
    def pic_split(origin: np.ndarray, column: int) -> typing.List[np.ndarray]:
        res = [
            np.hsplit(np.vsplit(origin, column)[i], column)
            for i in range(column)
        ]
        return [j for i in res for j in i]

    def convert_video_into_ssim_list(self, video_path: str, block: int = None, **kwargs) -> typing.List[VideoCutRange]:
        if not block:
            block = 2

        ssim_list = list()
        with toolbox.video_capture(video_path) as cap:
            # get video info
            frame_count = toolbox.get_frame_count(cap)
            frame_size = toolbox.get_frame_size(cap)
            logger.debug(f'total frame count: {frame_count}, size: {frame_size}')

            # load the first two frames
            _, start = cap.read()
            start_frame_id = toolbox.get_current_frame_id(cap)
            start_frame_time = toolbox.get_current_frame_time(cap)

            toolbox.video_jump(cap, self.step + 1)
            ret, end = cap.read()
            end_frame_id = toolbox.get_current_frame_id(cap)
            end_frame_time = toolbox.get_current_frame_time(cap)

            # compress
            start = toolbox.compress_frame(start, **kwargs)

            # split func
            # width > height
            if frame_size[0] > frame_size[1]:
                split_func = np.hsplit
            else:
                split_func = np.vsplit
            logger.debug(f'split function: {split_func.__name__}')

            while ret:
                end = toolbox.compress_frame(end, **kwargs)

                start_part_list = self.pic_split(start, block)
                end_part_list = self.pic_split(end, block)

                ssim = 1.
                for part_index, (each_start, each_end) in enumerate(zip(start_part_list, end_part_list)):
                    part_ssim = toolbox.compare_ssim(each_start, each_end)
                    if part_ssim < ssim:
                        ssim = part_ssim
                    logger.debug(f'part {part_index}: {part_ssim}')
                logger.debug(f'ssim between {start_frame_id} & {end_frame_id}: {ssim}')

                ssim_list.append(
                    VideoCutRange(
                        video_path,
                        start=start_frame_id,
                        end=end_frame_id,
                        ssim=[ssim],
                        start_time=start_frame_time,
                        end_time=end_frame_time,
                    )
                )

                # load the next one
                start = end
                start_frame_id, end_frame_id = end_frame_id, end_frame_id + self.step
                start_frame_time = end_frame_time
                toolbox.video_jump(cap, end_frame_id)
                ret, end = cap.read()
                end_frame_time = toolbox.get_current_frame_time(cap)

        return ssim_list

    def cut(self, video_path: str, **kwargs) -> VideoCutResult:
        """
        convert video file, into a VideoCutResult

        :param video_path: video file path
        :param kwargs: parameters of toolbox.compress_frame can be used here
        :return:
        """

        logger.info(f'start cutting: {video_path}')
        assert os.path.isfile(video_path), f'video [{video_path}] not existed'

        # if video contains 100 frames
        # it starts from 1, and length of list is 99, not 100
        # [SSIM(1-2), SSIM(2-3), SSIM(3-4) ... SSIM(99-100)]
        ssim_list = self.convert_video_into_ssim_list(video_path, **kwargs)
        logger.info(f'cut finished: {video_path}')

        # TODO other analysis results can be added to VideoCutResult, such as AI cutter?
        return VideoCutResult(
            video_path,
            ssim_list,
        )
