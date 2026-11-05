"""Same-origin local recording API; never starts/stops robot tracking."""
import asyncio
import json
from dataclasses import asdict

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from ..demo_recording import MAX_LOG_BYTES, MAX_SECONDS, MAX_VIDEO_BYTES, RecordingError


async def bounded_body(request, limit):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, '录制上传超过允许大小')
        data.extend(chunk)
    return bytes(data)


def attach_demo_recording_routes(app, runtime):
    service = runtime.demo_recorder

    @app.get('/api/demo-recordings')
    async def list_demo_recordings():
        capture = getattr(runtime.audio, 'capture', None)
        ready = capture.metrics().get('ok', False) if capture and hasattr(capture, 'metrics') else False
        return {'feature_version': 'D1', 'max_seconds': MAX_SECONDS, 'audio_ready': bool(ready),
                'recordings': await asyncio.to_thread(service.recent)}

    @app.post('/api/demo-recordings/start')
    async def start_demo_recording():
        try:
            result = await asyncio.to_thread(service.start,
                left_channel=runtime.audio_capture.ear_left_channel,
                right_channel=runtime.audio_capture.ear_right_channel,
                context={'algorithm_revision': runtime.snapshot(include_frame=False)['algorithm_revision'],
                         'features': asdict(runtime.features), 'running_at_start': runtime.running,
                         'axis_limits': asdict(runtime.axis_limits), 'direction': asdict(runtime.direction),
                         'audio_capture': asdict(runtime.audio_capture),
                         'vision_strategy': runtime.tracking_strategy})
            runtime.log('demo_recording_start', {'id': result['id']})
            return result
        except RecordingError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/demo-recordings/{identity}')
    async def demo_recording_state(identity: str):
        try:
            return await asyncio.to_thread(service.describe, identity)
        except (RecordingError, OSError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post('/api/demo-recordings/{identity}/stop')
    async def stop_demo_recording(identity: str):
        try:
            result = await asyncio.to_thread(service.stop, identity)
            runtime.log('demo_recording_stop', {'id': identity})
            return result
        except RecordingError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/demo-recordings/{identity}/timeline')
    async def save_demo_timeline(identity: str, request: Request):
        data = await bounded_body(request, MAX_LOG_BYTES)
        try:
            def reject_constant(_):
                raise ValueError('non-finite JSON')
            payload = json.loads(data, parse_constant=reject_constant)
            return await asyncio.to_thread(service.save_timeline, identity, payload)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put('/api/demo-recordings/{identity}/video')
    async def upload_demo_video(identity: str, request: Request):
        if request.headers.get('content-type', '').split(';', 1)[0].lower() != 'video/webm':
            raise HTTPException(415, '请上传浏览器生成的WebM视频')
        try:
            path = await asyncio.to_thread(service.begin_upload, identity)
        except RecordingError as exc:
            raise HTTPException(409, str(exc)) from exc
        success = False
        try:
            count = 0
            with path.open('xb') as target:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > MAX_VIDEO_BYTES:
                        raise HTTPException(413, '视频超过96 MiB，请缩短录制或降低分辨率')
                    await asyncio.to_thread(target.write, chunk)
            await asyncio.to_thread(service.finish_upload, identity, True)
            success = True
        except RecordingError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            if not success:
                await asyncio.to_thread(service.finish_upload, identity, False)
        return await asyncio.to_thread(service.describe, identity)

    @app.get('/api/demo-recordings/{identity}/download/{kind}')
    async def download_demo_recording(identity: str, kind: str):
        try:
            path, media_type = await asyncio.to_thread(service.download, identity, kind)
            return FileResponse(path, filename=f'deepsight-{identity[:8]}-{path.name}', media_type=media_type)
        except RecordingError as exc:
            raise HTTPException(404, str(exc)) from exc
