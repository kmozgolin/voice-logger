"""
Compatibility shim for torchaudio 2.6+ / PyTorch 2.6+ with pyannote.audio 3.x.
Must be imported before any pyannote import.
"""
import sys as _sys
import types as _types

# speechbrain uses LazyModule for optional integrations (k2, transformers, etc.).
# inspect.stack() triggers __getattr__ on every module in sys.modules, which
# causes LazyModule to try loading its target and crash when deps are missing.
# Fix: patch LazyModule.__getattr__ to return None for dunder attributes
# (like __file__) without attempting a real import.
try:
    from speechbrain.utils.importutils import LazyModule as _LazyModule
    _orig_lazy_getattr = _LazyModule.__getattr__
    def _safe_lazy_getattr(self, attr):
        if attr.startswith('__') and attr.endswith('__'):
            return None
        return _orig_lazy_getattr(self, attr)
    _LazyModule.__getattr__ = _safe_lazy_getattr
except Exception:
    pass

import torch as _torch
import torchaudio as _ta

# PyTorch 2.6+ changed torch.load default to weights_only=True.
# pyannote checkpoints contain TorchVersion and other trusted globals —
# patch torch.load to keep weights_only=False for backward compatibility.
_orig_load = _torch.load
def _patched_load(*args, **kwargs):
    if kwargs.get('weights_only') is None:
        kwargs['weights_only'] = False
    return _orig_load(*args, **kwargs)
_torch.load = _patched_load

if not hasattr(_ta, 'AudioMetaData'):
    from dataclasses import dataclass

    @dataclass
    class _AudioMetaData:
        sample_rate: int
        num_frames: int
        num_channels: int
        bits_per_sample: int = 16
        encoding: str = 'PCM_S'

    _ta.AudioMetaData = _AudioMetaData

if not hasattr(_ta, 'info'):
    import soundfile as _sf

    def _info(filepath, backend=None):
        info = _sf.info(str(filepath))
        return _ta.AudioMetaData(
            sample_rate=info.samplerate,
            num_frames=info.frames,
            num_channels=info.channels,
            bits_per_sample=16,
            encoding='PCM_S',
        )

    _ta.info = _info

if not hasattr(_ta, 'list_audio_backends'):
    _ta.list_audio_backends = lambda: ['soundfile']

# torchaudio 2.11 removed its own I/O backend — torchaudio.load() now requires
# torchcodec which is not installed. Replace with a soundfile implementation.
import soundfile as _sf

def _patched_ta_load(uri, frame_offset=0, num_frames=-1, normalize=True,
                     channels_first=True, format=None, buffer_size=4096, backend=None):
    import torch
    data, sr = _sf.read(
        str(uri), dtype='float32', always_2d=True,
        start=frame_offset,
        frames=num_frames,  # -1 means "read all" in both torchaudio and soundfile
    )
    # soundfile returns (frames, channels); torch expects (channels, frames)
    waveform = _torch.from_numpy(data.T if channels_first else data)
    return waveform, sr

_ta.load = _patched_ta_load
