from unittest.mock import Mock

import pytest
from yt_dlp.utils import PostProcessingError

from video_compat import CompatibleMP4PP
from yt_dlp import YoutubeDL


@pytest.mark.parametrize('codec,pixel,audio,expected', [
    ('hevc', 'yuv420p', 'aac', 'libx264'),
    ('h264', 'yuv420p10le', 'aac', 'libx264'),
    ('h264', 'yuv420p', 'opus', 'copy'),
    ('h264', 'yuv420p', None, None),
    ('h264', 'yuv420p', 'aac', None),
])
def test_codec_normalization(tmp_path, codec, pixel, audio, expected):
    source = tmp_path / 'video.mp4'
    pp = CompatibleMP4PP()
    streams = [{'index': 0, 'codec_type': 'video', 'codec_name': codec, 'pix_fmt': pixel}]
    if audio:
        streams.append({'index': 1, 'codec_type': 'audio', 'codec_name': audio})
    pp.get_metadata_object = Mock(return_value={'streams': streams})
    pp.run_ffmpeg = Mock()
    pp.to_screen = Mock()
    removed, result = pp.run({'filepath': str(source)})
    if expected is None:
        pp.run_ffmpeg.assert_not_called()
        assert removed == [] and result['filepath'] == str(source)
    else:
        args = pp.run_ffmpeg.call_args.args[2]
        assert args[args.index('-c:v') + 1] == expected
        assert args[args.index('-c:a') + 1] == ('aac' if audio == 'opus' else 'copy')
        assert result['filepath'].endswith('.compatible.mp4')
        assert removed == [str(source)]
        if expected == 'libx264':
            assert args[args.index('-preset') + 1] == 'ultrafast'
            assert args[args.index('-threads') + 1] == '2'


def test_compatible_source_preferred_before_conversion():
    from app import _run_job
    # Capture the actual download options, including sorting settings.
    import inspect, ast
    tree = ast.parse(inspect.getsource(_run_job))
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'options' for t in n.targets))
    values = dict(zip([k.value for k in assignment.value.keys], assignment.value.values))
    settings = {key: ast.literal_eval(values[key]) for key in ('format_sort', 'format_sort_force')}
    settings.update(quiet=True, format='bv*+ba/b')
    with YoutubeDL(settings) as ydl:
        result = ydl.process_ie_result({'id': 'test', 'title': 'test', 'formats': [
            {'format_id': 'hevc', 'url': 'https://example.com/hevc', 'vcodec': 'hevc',
             'acodec': 'aac', 'height': 1080, 'ext': 'mp4'},
            {'format_id': 'avc', 'url': 'https://example.com/avc', 'vcodec': 'avc1.640028',
             'acodec': 'mp4a.40.2', 'height': 720, 'ext': 'mp4'},
        ]}, download=False)
    assert result['format_id'] == 'avc'


def test_missing_video_fails():
    pp = CompatibleMP4PP()
    pp.get_metadata_object = Mock(return_value={'streams': []})
    with pytest.raises(PostProcessingError, match='No playable video'):
        pp.run({'filepath': 'video.mp4'})
