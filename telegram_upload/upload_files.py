import datetime
import math
import os


import mimetypes
from io import FileIO, SEEK_SET
from typing import Union, TYPE_CHECKING

import click
from hachoir.metadata.metadata import RootMetadata
from hachoir.metadata.video import MP4Metadata
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename

from telegram_upload.caption_formatter import CaptionFormatter, FilePath
from telegram_upload.exceptions import TelegramInvalidFile, ThumbError
from telegram_upload.utils import scantree, truncate
from telegram_upload.video import get_video_thumb, video_metadata

mimetypes.init()


if TYPE_CHECKING:
    from telegram_upload.client import TelegramManagerClient


def is_valid_file(file, error_logger=None):
    error_message = None
    if not os.path.lexists(file):
        error_message = 'File "{}" does not exist.'.format(file)
    elif not os.path.getsize(file):
        error_message = 'File "{}" is empty.'.format(file)
    if error_message and error_logger is not None:
        error_logger(error_message)
    return error_message is None


def get_file_mime(file):
    return (mimetypes.guess_type(file)[0] or ('')).split('/')[0]


def metadata_has(metadata: RootMetadata, key: str):
    try:
        return metadata.has(key)
    except ValueError:
        return False


def get_file_attributes(file):
    attrs = []
    mime = get_file_mime(file)
    if mime == 'video':
        metadata = video_metadata(file)
        video_meta = metadata
        meta_groups = None
        if hasattr(metadata, '_MultipleMetadata__groups'):
            # Is mkv
            meta_groups = metadata._MultipleMetadata__groups
        if metadata is not None and not metadata.has('width') and meta_groups:
            video_meta = meta_groups[next(filter(lambda x: x.startswith('video'), meta_groups._key_list))]
        if metadata is not None:
            supports_streaming = isinstance(video_meta, MP4Metadata)
            attrs.append(DocumentAttributeVideo(
                (0, metadata.get('duration').seconds)[metadata_has(metadata, 'duration')],
                (0, video_meta.get('width'))[metadata_has(video_meta, 'width')],
                (0, video_meta.get('height'))[metadata_has(video_meta, 'height')],
                False,
                supports_streaming,
            ))
    return attrs


def get_file_thumb(file):
    if get_file_mime(file) == 'video':
        return get_video_thumb(file)


class UploadFilesBase:
    def __init__(self, client: 'TelegramManagerClient', files, thumbnail: Union[str, bool, None] = None,
                 force_file: bool = False, caption: Union[str, None] = None):
        self._iterator = None
        self.client = client
        self.files = files
        self.thumbnail = thumbnail
        self.force_file = force_file
        self.caption = caption

    def get_iterator(self):
        raise NotImplementedError

    def __iter__(self):
        self._iterator = self.get_iterator()
        return self

    def __next__(self):
        if self._iterator is None:
            self._iterator = self.get_iterator()
        return next(self._iterator)


class RecursiveFiles(UploadFilesBase):

    def get_iterator(self):
        for file in self.files:
            if os.path.isdir(file):
                yield from map(lambda file: file.path,
                               filter(lambda x: not x.is_dir(), scantree(file, True)))
            else:
                yield file


class NoDirectoriesFiles(UploadFilesBase):
    def get_iterator(self):
        for file in self.files:
            if os.path.isdir(file):
                raise TelegramInvalidFile('"{}" is a directory.'.format(file))
            else:
                yield file


class LargeFilesBase(UploadFilesBase):
    def get_iterator(self):
        for file in self.files:
            if os.path.getsize(file) > self.client.max_file_size:
                yield from self.process_large_file(file)
            else:
                yield self.process_normal_file(file)

    def process_normal_file(self, file: str) -> 'File':
        return File(self.client, file, force_file=self.force_file, thumbnail=self.thumbnail, caption=self.caption)

    def process_large_file(self, file):
        raise NotImplementedError


class NoLargeFiles(LargeFilesBase):
    def process_large_file(self, file):
        raise TelegramInvalidFile('"{}" file is too large for Telegram.'.format(file))


class File(FileIO):
    force_file = False

    def __init__(self, client: 'TelegramManagerClient', path: str, force_file: Union[bool, None] = None,
                 thumbnail: Union[str, bool, None] = None, caption: Union[str, None] = None):
        super().__init__(path)
        self.client = client
        self.path = path
        self.force_file = self.force_file if force_file is None else force_file
        self._thumbnail = thumbnail
        # self._caption = caption
        self._caption = caption.encode('utf-8').decode('utf-8')

    @property
    def file_name(self):
        return os.path.basename(self.path)

    @property
    def file_size(self):
        return os.path.getsize(self.path)

    @property
    def short_name(self):
        return '.'.join(self.file_name.split('.')[:-1])

    @property
    def is_custom_thumbnail(self):
        return self._thumbnail is not False and self._thumbnail is not None

    @property
    def file_caption(self) -> str:
        """Get file caption. If caption parameter is not set, return file name.
        If caption is set, format it with CaptionFormatter.
        Anyways, truncate caption to max_caption_length.
        """
        if self._caption is not None:
            formatter = CaptionFormatter()
            caption = formatter.format(self._caption, file=FilePath(self.path), now=datetime.datetime.now())
        else:
            caption = self.short_name
        return truncate(caption, self.client.max_caption_length)

    def get_thumbnail(self):
        thumb = None
        if self._thumbnail is None and not self.force_file:
            try:
                thumb = get_file_thumb(self.path)
            except ThumbError as e:
                click.echo('{}'.format(e), err=True)
        elif self.is_custom_thumbnail:
            if not isinstance(self._thumbnail, str):
                raise TypeError('Invalid type for thumbnail: {}'.format(type(self._thumbnail)))
            elif not os.path.lexists(self._thumbnail):
                raise TelegramInvalidFile('{} thumbnail file does not exists.'.format(self._thumbnail))
            thumb = self._thumbnail
        return thumb

    @property
    def file_attributes(self):
        if self.force_file:
            return [DocumentAttributeFilename(self.file_name)]
        else:
            return get_file_attributes(self.path)


class SplitFile(File, FileIO):
    force_file = True

    def __init__(self, client: 'TelegramManagerClient', file: Union[str, bytes, int], max_read_size: int, name: str):
        super().__init__(client, file)
        self.max_read_size = max_read_size
        self.remaining_size = max_read_size
        self._name = name

    def read(self, size: int = -1) -> bytes:
        if size == -1:
            size = self.remaining_size
        if not self.remaining_size:
            return b''
        size = min(self.remaining_size, size)
        self.remaining_size -= size
        return super().read(size)

    def readall(self) -> bytes:
        return self.read()

    @property
    def file_name(self):
        return self._name

    @property
    def file_size(self):
        return self.max_read_size

    def seek(self, offset: int, whence: int = SEEK_SET, split_seek: bool = False) -> int:
        if not split_seek:
            self.remaining_size += self.tell() - offset
        return super().seek(offset, whence)

    @property
    def short_name(self):
        return self.file_name.split('/')[-1]


class SplitFiles(LargeFilesBase):
    def process_large_file(self, file):
        file_name = os.path.basename(file)
        total_size = os.path.getsize(file)
        parts = math.ceil(total_size / self.client.max_file_size)
        zfill = int(math.log10(10)) + 1
        for part in range(parts):
            size = total_size - (part * self.client.max_file_size) if part >= parts - 1 else self.client.max_file_size
            splitted_file = SplitFile(self.client, file, size, '{}.{}'.format(file_name, str(part).zfill(zfill)))
            splitted_file.seek(self.client.max_file_size * part, split_seek=True)
            yield splitted_file
