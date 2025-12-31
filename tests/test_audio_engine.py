import time

from Gamp import AudioEngine, Config


def test_simulate_audio_data_populates_buffer():
    cfg = Config()
    ae = AudioEngine(cfg)
    # Clear any existing buffer
    with ae.buffer_lock:
        ae.audio_buffer.clear()
    ae.current_bpm = 60.0
    ae.simulate_audio_data()
    # After a call, audio_buffer should have some samples
    with ae.buffer_lock:
        assert len(ae.audio_buffer) > 0


def test_beat_analysis_basic():
    cfg = Config()
    ae = AudioEngine(cfg)
    with ae.buffer_lock:
        ae.audio_buffer = [0.0]*2048
        # inject a simple bass-like pulse
        for i in range(512):
            ae.audio_buffer[-512 + i] = (1.0 if i % 64 == 0 else 0.0)
    # Run single analysis iteration
    ae._analyze_beats()
    # Should be safe to call and maintain attributes
    assert hasattr(ae, 'current_bpm')
    assert isinstance(ae.current_bpm, float)