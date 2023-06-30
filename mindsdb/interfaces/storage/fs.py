import os
import io
import fcntl
import shutil
import tarfile
import hashlib
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Union, Optional
from dataclasses import dataclass
from datetime import datetime
import threading

import psutil
from checksumdir import dirhash
try:
    import boto3
except Exception:
    # Only required for remote storage on s3
    pass

from mindsdb.utilities.config import Config
from mindsdb.utilities.context import context as ctx
import mindsdb.utilities.profiler as profiler


@dataclass(frozen=True)
class RESOURCE_GROUP:
    PREDICTOR = 'predictor'
    INTEGRATION = 'integration'
    TAB = 'tab'


RESOURCE_GROUP = RESOURCE_GROUP()


def copy(src, dst):
    if os.path.isdir(src):
        if os.path.exists(dst):
            if dirhash(src) == dirhash(dst):
                return
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
    else:
        if os.path.exists(dst):
            if hashlib.md5(open(src, 'rb').read()).hexdigest() == hashlib.md5(open(dst, 'rb').read()).hexdigest():
                return
        try:
            os.remove(dst)
        except Exception:
            pass
        shutil.copy2(src, dst)


class BaseFSStore(ABC):
    """Base class for file storage
    """

    def __init__(self):
        self.config = Config()
        self.storage = self.config['paths']['storage']

    @abstractmethod
    def get(self, local_name, base_dir):
        """Copy file/folder from storage to {base_dir}

        Args:
            local_name (str): name of resource (file/folder)
            base_dir (str): path to copy the resource
        """
        pass

    @abstractmethod
    def put(self, local_name, base_dir):
        """Copy file/folder from {base_dir} to storage

        Args:
            local_name (str): name of resource (file/folder)
            base_dir (str): path to folder with the resource
        """
        pass

    @abstractmethod
    def delete(self, remote_name):
        """Delete file/folder from storage

        Args:
            remote_name (str): name of resource
        """
        pass


def get_dir_size(path: str):
    total = 0
    with os.scandir(path) as it:
        for entry in it:
            if entry.is_file():
                total += entry.stat().st_size
            elif entry.is_dir():
                total += get_dir_size(entry.path)
    return total


class LocalFSStore(BaseFSStore):
    """Storage that stores files locally
    """

    def __init__(self):
        super().__init__()

    def get(self, local_name, base_dir):
        remote_name = local_name
        src = os.path.join(self.storage, remote_name)
        dest = os.path.join(base_dir, local_name)
        if not os.path.exists(dest) or get_dir_size(src) != get_dir_size(dest):
            copy(src, dest)

    def put(self, local_name, base_dir, compression_level):
        remote_name = local_name
        copy(
            os.path.join(base_dir, local_name),
            os.path.join(self.storage, remote_name)
        )

    def delete(self, remote_name):
        path = Path(self.storage).joinpath(remote_name)
        try:
            if path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except FileNotFoundError:
            pass


class FileLock:
    """ file lock to make safe concurrent access to directory
        works as context
    """

    def __init__(self, local_path: Path):
        """ Args:
            local_path (Path): path to directory
        """
        self._local_path = local_path
        self._local_file_name = 'dir.lock'
        self._lock_file_path = local_path / self._local_file_name

    def __enter__(self):
        if os.name != 'posix':
            return
        if self._lock_file_path.is_file() is False:
            try:
                self._local_path.mkdir(parents=True, exist_ok=True)
                self._lock_file_path.write_text('')
            except Exception:
                pass
        try:
            self._file = open(self._lock_file_path, 'r')
            fd = self._file.fileno()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ValueError, FileNotFoundError):
            # file probably was deleted between open and lock
            print(f'Cant accure lock on {self._local_path}')
            raise FileNotFoundError
        except BlockingIOError:
            print(f'Directory is locked by another process: {self._local_path}')
            fcntl.flock(fd, fcntl.LOCK_EX)

    def __exit__(self, exc_type, exc_value, traceback):
        if os.name != 'posix':
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
        except Exception:
            pass


class S3FSStore(BaseFSStore):
    """Storage that stores files in amazon s3
    """

    dt_format = '%d.%m.%y %H:%M:%S.%f'

    def __init__(self):
        super().__init__()
        if 's3_credentials' in self.config['permanent_storage']:
            self.s3 = boto3.client('s3', **self.config['permanent_storage']['s3_credentials'])
        else:
            self.s3 = boto3.client('s3')
        self.bucket = self.config['permanent_storage']['bucket']
        self._thread_lock = threading.Lock()

    def _get_remote_last_modified(self, object_name: str) -> datetime:
        """ get time when object was created/modified

            Args:
                object_name (str): name if file in bucket

            Returns:
                datetime
        """
        last_modified = self.s3.get_object_attributes(
            Bucket=self.bucket,
            Key=object_name,
            ObjectAttributes=['Checksum']
        )['LastModified']
        last_modified = last_modified.replace(tzinfo=None)
        return last_modified

    @profiler.profile()
    def _get_local_last_modified(self, base_dir: str, local_name: str) -> datetime:
        """ get 'last_modified' that saved locally

            Args:
                base_dir (str): path to base folder
                local_name (str): folder name

            Returns:
                datetime | None
        """
        last_modified_file_path = Path(base_dir) / local_name / 'last_modified.txt'
        if last_modified_file_path.is_file() is False:
            return None
        last_modified_text = last_modified_file_path.read_text()
        last_modified_datetime = datetime.strptime(last_modified_text, self.dt_format)
        return last_modified_datetime

    @profiler.profile()
    def _save_local_last_modified(self, base_dir: str, local_name: str, last_modified: datetime):
        """ Save 'last_modified' to local folder

            Args:
                base_dir (str): path to base folder
                local_name (str): folder name
                last_modified (datetime)
        """
        last_modified_file_path = Path(base_dir) / local_name / 'last_modified.txt'
        last_modified_text = last_modified.strftime(self.dt_format)
        last_modified_file_path.write_text(last_modified_text)

    @profiler.profile()
    def _download(self, base_dir: str, remote_ziped_name: str,
                  local_ziped_path: str, last_modified: datetime = None):
        """ download file to s3 and unarchive it

            Args:
                base_dir (str)
                remote_ziped_name (str)
                local_ziped_path (str)
                last_modified (datetime, optional)
        """
        os.makedirs(base_dir, exist_ok=True)
        self.s3.download_file(self.bucket, remote_ziped_name, local_ziped_path)
        shutil.unpack_archive(local_ziped_path, base_dir)
        os.system(f'chmod -R 777 {base_dir}')
        os.remove(local_ziped_path)

        if last_modified is None:
            last_modified = self._get_remote_last_modified(remote_ziped_name)
        self._save_local_last_modified(
            base_dir,
            remote_ziped_name.replace('tar.gz', ''),
            last_modified
        )

    @profiler.profile()
    def get(self, local_name, base_dir):
        remote_name = local_name
        remote_ziped_name = f'{remote_name}.tar.gz'
        local_ziped_name = f'{local_name}.tar.gz'
        local_ziped_path = os.path.join(base_dir, local_ziped_name)

        local_last_modified = self._get_local_last_modified(base_dir, local_name)
        remote_last_modified = self._get_remote_last_modified(remote_ziped_name)
        if (
            local_last_modified is not None
            and local_last_modified == remote_last_modified
        ):
            return

        self._download(
            base_dir,
            remote_ziped_name,
            local_ziped_path,
            last_modified=remote_last_modified
        )

    @profiler.profile()
    def put(self, local_name, base_dir, compression_level=9):
        # NOTE: This `make_archive` function is implemente poorly and will create an empty archive file even if
        # the file/dir to be archived doesn't exist or for some other reason can't be archived
        remote_name = local_name
        remote_zipped_name = f'{remote_name}.tar.gz'

        old_cwd = os.getcwd()
        fh = io.BytesIO()
        dir_path = Path(base_dir) / remote_name
        dir_size = sum(f.stat().st_size for f in dir_path.glob('**/*') if f.is_file())
        if (dir_size * 2) < psutil.virtual_memory().available:
            with self._thread_lock:
                os.chdir(base_dir)
                with tarfile.open(fileobj=fh, mode='w:gz', compresslevel=compression_level) as tar:
                    for path in dir_path.iterdir():
                        if path.is_file() and path.name in ('dir.lock', 'last_modified.txt'):
                            pass
                        tar.add(path.name)
                os.chdir(old_cwd)

            self.s3.upload_fileobj(
                fh,
                self.bucket,
                remote_zipped_name
            )
        else:
            shutil.make_archive(
                os.path.join(base_dir, remote_name),
                'gztar',
                root_dir=base_dir,
                base_dir=local_name
            )
            self.s3.upload_file(
                os.path.join(base_dir, remote_zipped_name),
                self.bucket,
                remote_zipped_name
            )
            os.remove(os.path.join(base_dir, remote_zipped_name))

        last_modified = self._get_remote_last_modified(remote_zipped_name)
        self._save_local_last_modified(base_dir, local_name, last_modified)

    @profiler.profile()
    def delete(self, remote_name):
        self.s3.delete_object(Bucket=self.bucket, Key=remote_name)


def FsStore():
    storage_location = Config()['permanent_storage']['location']
    if storage_location == 'local':
        return LocalFSStore()
    elif storage_location == 's3':
        return S3FSStore()
    else:
        raise Exception(f"Location: '{storage_location}' not supported")


class FileStorage:
    def __init__(self, resource_group: str, resource_id: int,
                 root_dir: str = 'content', sync: bool = True):
        """
            Args:
                resource_group (str)
                resource_id (int)
                root_dir (str)
                sync (bool)
        """

        self.resource_group = resource_group
        self.resource_id = resource_id
        self.root_dir = root_dir
        self.sync = sync

        self.folder_name = f'{resource_group}_{ctx.company_id}_{resource_id}'

        config = Config()
        self.fs_store = FsStore()
        self.content_path = Path(config['paths'][root_dir])
        self.resource_group_path = self.content_path / resource_group
        self.folder_path = self.resource_group_path / self.folder_name
        if self.folder_path.exists() is False:
            self.folder_path.mkdir(parents=True, exist_ok=True)

    @profiler.profile()
    def push(self, compression_level=9):
        with FileLock(self.folder_path):
            self._push_no_lock(compression_level=compression_level)

    @profiler.profile()
    def _push_no_lock(self, compression_level=9):
        self.fs_store.put(
            str(self.folder_name),
            str(self.resource_group_path),
            compression_level=compression_level
        )

    @profiler.profile()
    def push_path(self, path):
        with FileLock(self.folder_path):
            self.fs_store.put(os.path.join(self.folder_name, path), str(self.resource_group_path))

    @profiler.profile()
    def pull(self):
        with FileLock(self.folder_path):
            self._pull_no_lock()

    @profiler.profile()
    def _pull_no_lock(self):
        try:
            self.fs_store.get(str(self.folder_name), str(self.resource_group_path))
        except Exception:
            pass

    @profiler.profile()
    def pull_path(self, path, update=True):
        with FileLock(self.folder_path):
            if update is False:
                # not pull from source if object is exists
                if os.path.exists(self.resource_group_path / self.folder_name / path):
                    return
            try:
                # TODO not sync if not changed?
                self.fs_store.get(os.path.join(self.folder_name, path), str(self.resource_group_path))
            except Exception:
                pass

    @profiler.profile()
    def file_set(self, name, content):
        with FileLock(self.folder_path):
            if self.sync is True:
                self._pull_no_lock()

            dest_abs_path = self.folder_path / name

            with open(dest_abs_path, 'wb') as fd:
                fd.write(content)

            if self.sync is True:
                self._push_no_lock()

    @profiler.profile()
    def file_get(self, name):
        with FileLock(self.folder_path):
            if self.sync is True:
                self._pull_no_lock()

            dest_abs_path = self.folder_path / name

            with open(dest_abs_path, 'rb') as fd:
                return fd.read()

    @profiler.profile()
    def add(self, path: Union[str, Path], dest_rel_path: Optional[Union[str, Path]] = None):
        """Copy file/folder to persist storage

        Examples:
            Copy file 'args.json' to '{storage}/args.json'
            >>> fs.add('/path/args.json')

            Copy file 'args.json' to '{storage}/folder/opts.json'
            >>> fs.add('/path/args.json', 'folder/opts.json')

            Copy folder 'folder' to '{storage}/folder'
            >>> fs.add('/path/folder')

            Copy folder 'folder' to '{storage}/path/folder'
            >>> fs.add('/path/folder', 'path/folder')

        Args:
            path (Union[str, Path]): path to the resource
            dest_rel_path (Optional[Union[str, Path]]): relative path in storage to file or folder
        """
        with FileLock(self.folder_path):
            if self.sync is True:
                self._pull_no_lock()

            path = Path(path)
            if isinstance(dest_rel_path, str):
                dest_rel_path = Path(dest_rel_path)

            if dest_rel_path is None:
                dest_abs_path = self.folder_path / path.name
            else:
                dest_abs_path = self.folder_path / dest_rel_path

            copy(
                str(path),
                str(dest_abs_path)
            )

            if self.sync is True:
                self._push_no_lock()

    @profiler.profile()
    def get_path(self, relative_path: Union[str, Path]) -> Path:
        """ Return path to file or folder

        Examples:
            get path to 'opts.json':
            >>> fs.get_path('folder/opts.json')
            ... /path/{storage}/folder/opts.json

        Args:
            relative_path (Union[str, Path]): Path relative to the storage folder

        Returns:
            Path: path to requested file or folder
        """
        with FileLock(self.folder_path):
            if self.sync is True:
                self._pull_no_lock()

            if isinstance(relative_path, str):
                relative_path = Path(relative_path)
            # relative_path = relative_path.resolve()

            if relative_path.is_absolute():
                raise TypeError('FSStorage.get_path() got absolute path as argument')

            ret_path = self.folder_path / relative_path
            if not ret_path.exists():
                # raise Exception('Path does not exists')
                os.makedirs(ret_path)

        return ret_path

    def delete(self, relative_path: Union[str, Path] = '.'):
        with FileLock(self.folder_path):
            if isinstance(relative_path, str):
                relative_path = Path(relative_path)

            if relative_path.is_absolute():
                raise TypeError('FSStorage.delete() got absolute path as argument')

            path = (self.folder_path / relative_path).resolve()

            if path == self.folder_path.resolve():
                self._complete_removal()
                return

            if self.sync is True:
                self._pull_no_lock()

            if path.exists() is False:
                raise Exception('Path does not exists')

            if path.is_file():
                path.unlink()
            else:
                path.rmdir()

            if self.sync is True:
                self._push_no_lock()

    def _complete_removal(self):
        self.fs_store.delete(self.folder_name)
        shutil.rmtree(str(self.folder_path))


class FileStorageFactory:
    def __init__(self, resource_group: str,
                 root_dir: str = 'content', sync: bool = True):
        self.resource_group = resource_group
        self.root_dir = root_dir
        self.sync = sync

    def __call__(self, resource_id: int):
        return FileStorage(
            resource_group=self.resource_group,
            root_dir=self.root_dir,
            sync=self.sync,
            resource_id=resource_id
        )
