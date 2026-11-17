"""One bounded utterance per process. Model paths are supplied only by the server."""

import io
import json
import sys
import wave
from pathlib import Path


def main():
    import onnxruntime
    from piper import PiperVoice
    from piper.config import PiperConfig
    # Explicitly limit ONNX thread pools so concurrent Ollama gets CPU time.
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    config = PiperConfig.from_dict(json.loads(Path(sys.argv[1]+'.json').read_text(encoding='utf-8')))
    voice = PiperVoice(config=config, session=onnxruntime.InferenceSession(
        sys.argv[1], sess_options=options, providers=['CPUExecutionProvider']))
    text = sys.stdin.buffer.read(12000).decode('utf-8')
    if not 0 < len(text) <= 2000:
        raise ValueError('Text limit')
    out = io.BytesIO()
    with wave.open(out,'wb') as wav:
        voice.synthesize_wav(text,wav)
    sys.stdout.buffer.write(out.getvalue())


if __name__ == '__main__':
    main()
