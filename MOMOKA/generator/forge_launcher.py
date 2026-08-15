"""Helper utilities to manage Stable Diffusion WebUI Forge background process."""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ForgeLaunchConfig:
    base_url: str
    start_script: Path
    skip_pip_install: bool = True
    commandline_args: Optional[str] = None
    api_port: Optional[int] = None
    extra_env: Optional[Dict[str, str]] = None


class ForgeProcessManager:
    """Manage lifecycle of the Forge API subprocess."""

    def __init__(self, config: ForgeLaunchConfig) -> None:
        self._config = config
        self._process: Optional[subprocess.Popen] = None
        self._async_lock = asyncio.Lock()
        self._start_attempted = False
        atexit.register(self.stop)

    @property
    def config(self) -> ForgeLaunchConfig:
        return self._config

    async def ensure_running(self, timeout: float = 180.0) -> None:
        if await self._is_server_available():
            return

        async with self._async_lock:
            if await self._is_server_available():
                return

            if not self._process or self._process.poll() is not None:
                logger.info("Starting Stable Diffusion WebUI Forge via %s", self._config.start_script)
                await asyncio.to_thread(self._launch_subprocess)
                self._start_attempted = True

            try:
                # ready 待機
                await self._wait_until_ready(timeout)
            except Exception:
                # timeout / 失敗時は孤児 process を回収する
                self.stop()
                # 呼び出し元へ再送出
                raise

    async def _is_server_available(self) -> bool:
        target_url = urljoin(self._config.base_url.rstrip("/"), "/sdapi/v1/sd-models")
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(target_url) as response:
                    if response.status == 200:
                        return True
        except aiohttp.ClientError:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.debug("Unexpected error probing Forge API: %s", exc)
            return False
        return False

    def _launch_subprocess(self) -> None:
        env = os.environ.copy()
        if self._config.extra_env:
            env.update(self._config.extra_env)

        if self._config.skip_pip_install:
            env.setdefault("FORGE_SKIP_PIP", "1")

        if self._config.api_port:
            env.setdefault("FORGE_API_PORT", str(self._config.api_port))

        if self._config.commandline_args:
            env.setdefault("FORGE_COMMANDLINE_ARGS", self._config.commandline_args)

        env.setdefault("PYTHONIOENCODING", "utf-8")

        creationflags = 0
        if os.name == "nt":  # Windows-specific flag to allow graceful termination
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            command = ["cmd.exe", "/c", str(self._config.start_script)] if os.name == "nt" else [str(self._config.start_script)]
            self._process = subprocess.Popen(
                command,
                cwd=str(self._config.start_script.parent),
                env=env,
                stdout=None,
                stderr=None,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Forge start script not found: {self._config.start_script}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to launch Forge start script: {exc}") from exc

    async def _wait_until_ready(self, timeout: float) -> None:
        target_url = urljoin(self._config.base_url.rstrip("/"), "/sdapi/v1/sd-models")
        deadline = time.monotonic() + timeout

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            while time.monotonic() < deadline:
                try:
                    async with session.get(target_url) as response:
                        if response.status == 200:
                            logger.info("Forge API is ready at %s", self._config.base_url)
                            return
                except aiohttp.ClientError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Waiting for Forge API...")
                    logger.trace(exc) if hasattr(logger, "trace") else None

                await asyncio.sleep(3)

        raise TimeoutError(
            f"Forge API did not become ready within {timeout} seconds at {self._config.base_url}."
        )

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            logger.info("Stopping Forge subprocess")
            if os.name == "nt":
                try:
                    self._process.send_signal(signal.CTRL_BREAK_EVENT)
                except ValueError:
                    self._process.terminate()
            else:
                self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self._process.kill()
        self._process = None


_manager: Optional[ForgeProcessManager] = None


def get_forge_process_manager(config: ForgeLaunchConfig) -> ForgeProcessManager:
    global _manager

    if _manager is None:
        _manager = ForgeProcessManager(config)
    else:
        # Update configuration if base URL or script differs.
        if _manager.config != config:
            logger.info("Updating Forge process manager configuration: %s", config)
            _manager.stop()
            _manager = ForgeProcessManager(config)
    return _manager
