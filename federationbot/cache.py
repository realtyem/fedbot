from typing import Any, Callable, Dict, Generic, Optional, TypeVar
import asyncio
import time

KT = TypeVar("KT")
VT = TypeVar("VT")
CACHE_EXPIRY_TIME_SECONDS = 30 * 60  # 30 minutes
CACHE_CLEANUP_SLEEP_TIME_SECONDS = 30.0  # 30 seconds


class CacheEntry(Generic[VT]):
    cache_value: VT


class LRUCacheEntry(CacheEntry[VT]):
    last_access_time_ms: float


class LRUCache(Generic[KT, VT]):
    """

    Attributes:
        time_cb: A saved reference to time.time(). Gives a float
        eviction_condition_cb: The condition to check for eviction.
        expire_time_seconds: The time(float) an entry is allowed to stay for(default
            30 minutes)
        cleanup_iteration_sleep_seconds: How long the background cleanup task should
            sleep for between iterations(default 30.0 seconds)
        _cleanup_task:
    """

    def __init__(
        self,
        expire_after_seconds: float = CACHE_EXPIRY_TIME_SECONDS,
        cleanup_task_sleep_time_seconds: float = CACHE_CLEANUP_SLEEP_TIME_SECONDS,
        eviction_condition_func: Optional[Callable[[LRUCacheEntry], bool]] = None,
    ) -> None:
        """

        Args:
            expire_after_seconds:
            cleanup_task_sleep_time_seconds:
        """
        cache: Dict[KT, LRUCacheEntry[VT]] = dict()
        self._cache = cache
        self.time_cb = time.time
        self.eviction_condition_cb = (
            eviction_condition_func or self.default_expiry_condition
        )
        self.expire_time_seconds = expire_after_seconds
        self.cleanup_iteration_sleep_seconds = cleanup_task_sleep_time_seconds
        self._cleanup_task = asyncio.create_task(self._cleanup_cache_task())
        self.get = self.__getitem__
        self.set = self.__setitem__

    def __setitem__(self, key: KT, value: VT) -> None:
        cache_entry = self._cache.setdefault(key, LRUCacheEntry())
        cache_entry.cache_value = value
        cache_entry.last_access_time_ms = self.time_cb()
        self._cache[key] = cache_entry

    def __getitem__(self, item: KT, _default: Optional[VT] = None) -> Optional[VT]:
        if item in self._cache:
            self._cache[item].last_access_time_ms = self.time_cb()
            cache_entry = self._cache.get(item)
            assert cache_entry is not None
            return cache_entry.cache_value

        return _default

    def __len__(self) -> int:
        return len(self._cache)

    def default_expiry_condition(self, cache_entry: LRUCacheEntry) -> bool:
        return (
            self.time_cb() > cache_entry.last_access_time_ms + self.expire_time_seconds
        )

    async def _cleanup_cache_task(self) -> None:
        while True:
            # Wait for the sleep time defined at the start, to avoid an early spike
            await asyncio.sleep(self.cleanup_iteration_sleep_seconds)
            # Take a copy of the dict, as items() doesn't like it's view being changed
            # while it's still watching
            for cache_key, cache_entry in dict(self._cache).items():
                if self.eviction_condition_cb(cache_entry):
                    self._cache.pop(cache_key, None)

    async def stop(self) -> None:
        self._cleanup_task.cancel()
        await asyncio.gather(self._cleanup_task, return_exceptions=True)
