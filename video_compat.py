"""Normalize downloaded video codecs for common phone and desktop players."""
from pathlib import Path

from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from yt_dlp.utils import PostProcessingError


class CompatibleMP4PP(FFmpegPostProcessor):
    def run(self, info):
        source = Path(info['filepath'])
        streams = self.get_metadata_object(str(source)).get('streams', [])
        video = next((s for s in streams if s.get('codec_type') == 'video'
                      and not s.get('disposition', {}).get('attached_pic')), None)
        audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
        if video is None:
            raise PostProcessingError('No playable video stream was found')
        copy_video = (video.get('codec_name') == 'h264'
                      and video.get('pix_fmt') == 'yuv420p')
        copy_audio = audio is None or audio.get('codec_name') == 'aac'
        if source.suffix.lower() == '.mp4' and copy_video and copy_audio:
            return [], info

        target = source.with_name(source.stem + '.compatible.mp4')
        opts = ['-map', f"0:{video['index']}", '-map', '0:a:0?',
                '-c:v', 'copy' if copy_video else 'libx264']
        if not copy_video:
            opts += ['-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
                     '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2']
        opts += ['-c:a', 'copy' if copy_audio else 'aac']
        if not copy_audio:
            opts += ['-b:a', '192k']
        opts += ['-movflags', '+faststart']
        self.to_screen('Preparing a compatible MP4 video')
        self.run_ffmpeg(str(source), str(target), opts)
        info.update(filepath=str(target), ext='mp4', vcodec='h264')
        if audio:
            info['acodec'] = 'aac'
        return [str(source)], info
