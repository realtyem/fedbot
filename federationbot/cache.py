from typing import Dict, Generic, Optional, TypeVar
import asyncio
import time

KT = TypeVar("KT")
VT = TypeVar("VT")
CACHE_EXPIRY_TIME_SECONDS = 30 * 60  # 30 minutes
CACHE_CLEANUP_SLEEP_TIME_SECONDS = 30.0  # 30 seconds


class LRUCacheEntry(Generic[VT]):
    cache_value: VT
    last_access_time_ms: float


class LRUCache(Generic[KT, VT]):
    def __init__(
        self,
        expire_after_seconds: float = CACHE_EXPIRY_TIME_SECONDS,
        iteration_sleep_time_for_checking_expiration: float = CACHE_CLEANUP_SLEEP_TIME_SECONDS,
    ) -> None:
        cache: Dict[KT, LRUCacheEntry[VT]] = dict()
        self._cache = cache
        self.time_cb = time.time
        self.expire_time_seconds = expire_after_seconds
        self.cleanup_iteration_sleep_seconds = (
            iteration_sleep_time_for_checking_expiration
        )
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

    async def _cleanup_cache_task(self) -> None:
        while True:
            # Wait for the sleep time defined at the start, to avoid an early spike
            await asyncio.sleep(self.cleanup_iteration_sleep_seconds)
            time_now = self.time_cb()
            # Take a copy of the dict, as items() doesn't like it's view being changed
            # while it's still watching
            for cache_key, cache_entry in dict(self._cache).items():
                if (
                    time_now
                    > cache_entry.last_access_time_ms + self.expire_time_seconds
                ):
                    self._cache.pop(cache_key, None)

    async def stop(self) -> None:
        self._cleanup_task.cancel()
        await asyncio.gather(self._cleanup_task, return_exceptions=True)
