"""Style-Bert-VITS2 合成を Bot 本体から切り離して実行する子プロセスと、その操作クライアント。

子プロセスは ``python -m MOMOKA.generator.tts.worker <config-json>`` で起動し、
stdin / stdout で次のフレームをやり取りする（いずれも 4 バイト big-endian の長さ接頭辞付き）。

- 要求: JSON（text, style, style_weight, speed, noise_scale, noise_w, length_scale）
- 応答: JSON ヘッダ（ok, error, size）に続けて size バイトの WAV

ログは stderr に ``LEVEL|logger|message`` 形式で出し、親が自分のロガーへ転送する。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

_LEN = struct.Struct(">I")
# 子プロセスのログ行の区切り
_LOG_SEP = "|"
# プロジェクトルート（MOMOKA/generator/tts/worker.py → parents[3]）
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

logger = logging.getLogger("MOMOKA.tts.worker")


# ---------------------------------------------------------------------------
# 子プロセス側
# ---------------------------------------------------------------------------

def _read_exact(stream, size: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _write_frame(stream, header: Dict[str, Any], payload: bytes = b"") -> None:
    raw = json.dumps(header).encode("utf-8")
    stream.write(_LEN.pack(len(raw)) + raw + payload)
    stream.flush()


def _worker_main(config_json: str) -> int:
    # プロトコル用に本物の stdout を退避し、以降の print / C 拡張の出力は stderr へ流す
    proto_out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    proto_in = sys.stdin.buffer

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format=f"%(levelname)s{_LOG_SEP}%(name)s{_LOG_SEP}%(message)s",
    )

    # C10 二重登録を避けるため Style-Bert-VITS2 より先に torch を確定させる
    try:
        import torch  # noqa: F401
    except ImportError:
        pass

    from MOMOKA.generator.tts.synthesizer import StyleBertVITS2Synthesizer, SynthesizerConfig

    synthesizer = StyleBertVITS2Synthesizer(SynthesizerConfig(**json.loads(config_json)))
    logger.info("TTS worker ready (pid=%s)", os.getpid())

    while True:
        head = _read_exact(proto_in, _LEN.size)
        if head is None:
            # 親が stdin を閉じた（アイドル終了 / Bot 停止）
            break
        body = _read_exact(proto_in, _LEN.unpack(head)[0])
        if body is None:
            break
        try:
            req = json.loads(body.decode("utf-8"))
            wav = synthesizer.synthesize_to_wav(
                text=req["text"],
                style=req.get("style"),
                style_weight=float(req.get("style_weight", 5.0)),
                speed=float(req.get("speed", 1.0)),
                noise_scale=req.get("noise_scale"),
                noise_w=req.get("noise_w"),
                length_scale=req.get("length_scale"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("TTS synthesis failed: %s", exc)
            _write_frame(proto_out, {"ok": False, "error": str(exc), "size": 0})
            continue
        _write_frame(proto_out, {"ok": True, "size": len(wav)}, wav)

    logger.info("TTS worker exiting (pid=%s)", os.getpid())
    return 0


# ---------------------------------------------------------------------------
# 親（Bot）側
# ---------------------------------------------------------------------------

class TTSWorkerError(RuntimeError):
    pass


class TTSWorkerClient:
    """TTS ワーカープロセスを必要時に起動し、アイドルが続いたら終了させる。

    プロセスごと終了するため、torch / BERT が掴んだ RAM・VRAM も OS へ確実に返る。
    """

    def __init__(
        self,
        synth_config,
        *,
        idle_shutdown_seconds: float = 60.0,
        first_request_timeout: float = 180.0,
        request_timeout: float = 60.0,
    ) -> None:
        self._config_json = json.dumps(asdict(synth_config))
        self.idle_shutdown_seconds = float(idle_shutdown_seconds)
        # 初回はプロセス起動・torch import・モデル読込を含むため長めに待つ
        self.first_request_timeout = float(first_request_timeout)
        self.request_timeout = float(request_timeout)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._served_requests = 0
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def synthesize(self, **request: Any) -> bytes:
        async with self._lock:
            self._cancel_idle_shutdown()
            try:
                wav = await self._request(request)
            except TTSWorkerError:
                raise
            except Exception as exc:
                # 応答途中で壊れたプロセスは再利用しない
                await self._terminate()
                raise TTSWorkerError(f"TTS worker failed: {exc}") from exc
            finally:
                self._schedule_idle_shutdown()
            return wav

    async def close(self) -> None:
        self._cancel_idle_shutdown()
        async with self._lock:
            await self._terminate()

    async def _request(self, request: Dict[str, Any]) -> bytes:
        if not self.is_running:
            await self._spawn()
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        timeout = self.first_request_timeout if self._served_requests == 0 else self.request_timeout

        raw = json.dumps(request).encode("utf-8")
        proc.stdin.write(_LEN.pack(len(raw)) + raw)
        await proc.stdin.drain()

        async def _read_response() -> bytes:
            head = await proc.stdout.readexactly(_LEN.size)
            header = json.loads(await proc.stdout.readexactly(_LEN.unpack(head)[0]))
            payload = await proc.stdout.readexactly(int(header.get("size", 0)))
            if not header.get("ok"):
                raise TTSWorkerError(header.get("error") or "unknown TTS worker error")
            return payload

        try:
            wav = await asyncio.wait_for(_read_response(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"no response within {timeout:.0f}s") from exc
        self._served_requests += 1
        return wav

    async def _spawn(self) -> None:
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            # 親コンソールの Ctrl+C を受けず、停止は親が stdin を閉じて行う
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "MOMOKA.generator.tts.worker",
            self._config_json,
            cwd=str(_PROJECT_ROOT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        self._served_requests = 0
        self._stderr_task = asyncio.create_task(
            self._pump_stderr(self._proc), name="tts-worker-stderr"
        )
        logger.info("Started TTS worker process (pid=%s)", self._proc.pid)

    async def _pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            parts = text.split(_LOG_SEP, 2)
            level = logging.getLevelName(parts[0]) if len(parts) == 3 else logging.INFO
            if not isinstance(level, int):
                level = logging.INFO
            message = parts[2] if len(parts) == 3 else text
            logger.log(level, "%s", message)

    def _cancel_idle_shutdown(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _schedule_idle_shutdown(self) -> None:
        if not self.is_running:
            return
        self._cancel_idle_shutdown()
        self._idle_task = asyncio.create_task(
            self._shutdown_when_idle(), name="tts-worker-idle-shutdown"
        )

    async def _shutdown_when_idle(self) -> None:
        await asyncio.sleep(max(0.0, self.idle_shutdown_seconds))
        async with self._lock:
            # ロック待ちの間に合成が走った場合は、その合成がこのタスクを cancel している
            logger.info("TTS worker idle; stopping it to release memory")
            await self._terminate()

    async def _terminate(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                # stdin EOF で子はループを抜けて自発終了する
                if proc.stdin is not None:
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except Exception:  # noqa: BLE001
                    pass
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=2.0)
            except Exception:  # noqa: BLE001
                self._stderr_task.cancel()
            self._stderr_task = None


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv[1] if len(sys.argv) > 1 else "{}"))
