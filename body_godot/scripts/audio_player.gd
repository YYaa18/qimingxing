extends Node
class_name QimingxingAudioPlayer

signal amplitude_changed(rms: float)
signal speaking_state_changed(is_speaking: bool)

@export var earcons_enabled: bool = true
@export var ambient_duck_db: float = -12.0
@export var ambient_base_db: float = -18.0

var _tts_player: AudioStreamPlayer
var _tts_gen: AudioStreamGenerator
var _tts_playback: AudioStreamGeneratorPlayback

var _earcon_player: AudioStreamPlayer
var _earcon_gen: AudioStreamGenerator
var _earcon_playback: AudioStreamGeneratorPlayback

var _ambient_player: AudioStreamPlayer
var _ambient_gen: AudioStreamGenerator
var _ambient_playback: AudioStreamGeneratorPlayback
var _ambient_enabled: bool = false
var _ambient_lpf_state: float = 0.0

var _pending_samples: PackedFloat32Array = PackedFloat32Array()
var _read_pos: int = 0
var _is_streaming: bool = false
var _tail_cooldown: float = 0.0
var _duck_tween: Tween


func _ready() -> void:
	_tts_gen = AudioStreamGenerator.new()
	_tts_gen.mix_rate = 24000.0
	_tts_gen.buffer_length = 0.8
	_tts_player = AudioStreamPlayer.new()
	_tts_player.stream = _tts_gen
	_tts_player.bus = &"TTS"
	add_child(_tts_player)

	_earcon_gen = AudioStreamGenerator.new()
	_earcon_gen.mix_rate = 24000.0
	_earcon_gen.buffer_length = 0.35
	_earcon_player = AudioStreamPlayer.new()
	_earcon_player.stream = _earcon_gen
	_earcon_player.bus = &"Earcons"
	add_child(_earcon_player)
	_earcon_player.play()
	_earcon_playback = _earcon_player.get_stream_playback() as AudioStreamGeneratorPlayback

	_ambient_gen = AudioStreamGenerator.new()
	_ambient_gen.mix_rate = 24000.0
	_ambient_gen.buffer_length = 0.4
	_ambient_player = AudioStreamPlayer.new()
	_ambient_player.stream = _ambient_gen
	_ambient_player.bus = &"Ambient"
	add_child(_ambient_player)


func start_tts_stream(sample_rate: int = 24000) -> void:
	var target_rate := float(maxi(8000, sample_rate))
	_pending_samples.clear()
	_read_pos = 0
	_tail_cooldown = 0.0

	if absf(_tts_gen.mix_rate - target_rate) > 1.0:
		_tts_player.stop()
		_tts_gen.mix_rate = target_rate

	if not _tts_player.playing:
		_tts_player.play()
	_tts_playback = _tts_player.get_stream_playback() as AudioStreamGeneratorPlayback
	if _tts_playback != null:
		_tts_playback.clear_buffer()

	_is_streaming = true
	_set_ambient_ducked(true)
	speaking_state_changed.emit(true)


func push_pcm_chunk(data_b64: String) -> void:
	if data_b64 == "":
		return
	var raw: PackedByteArray = Marshalls.base64_to_raw(data_b64)
	var sample_count := raw.size() / 2
	if sample_count <= 0:
		return

	var start_len := _pending_samples.size()
	_pending_samples.resize(start_len + sample_count)
	for i in range(sample_count):
		var s16: int = raw.decode_s16(i * 2)
		_pending_samples[start_len + i] = clampf(float(s16) / 32768.0, -1.0, 1.0)


func finish_tts_stream(finish_reason: String = "completed") -> void:
	if finish_reason == "cancelled":
		stop_tts_immediately()
		return
	_is_streaming = false
	_tail_cooldown = 0.25


func stop_tts_immediately() -> void:
	_pending_samples.clear()
	_read_pos = 0
	_is_streaming = false
	_tail_cooldown = 0.0
	if _tts_playback != null:
		_tts_playback.clear_buffer()
	if _tts_player.playing:
		_tts_player.stop()
	amplitude_changed.emit(0.0)
	_set_ambient_ducked(false)
	speaking_state_changed.emit(false)


func _process(delta: float) -> void:
	_pump_tts_buffer(delta)
	_pump_ambient_noise()


func _pump_tts_buffer(delta: float) -> void:
	if _tts_playback == null:
		return
	var queued := _pending_samples.size() - _read_pos
	if queued > 0:
		var avail := _tts_playback.get_frames_available()
		var to_push := mini(queued, avail)
		if to_push > 0:
			var frames := PackedVector2Array()
			frames.resize(to_push)
			var sq_sum := 0.0
			for i in range(to_push):
				var v := _pending_samples[_read_pos + i]
				frames[i] = Vector2(v, v)
				sq_sum += v * v
			_tts_playback.push_buffer(frames)
			_read_pos += to_push
			var rms := sqrt(sq_sum / float(to_push))
			amplitude_changed.emit(clampf(rms * 3.5, 0.0, 1.0))

			# Reclaim consumed prefix periodically
			if _read_pos > 16384:
				_pending_samples = _pending_samples.slice(_read_pos)
				_read_pos = 0
		return

	if not _is_streaming and _tts_player.playing:
		_tail_cooldown -= delta
		if _tail_cooldown <= 0.0:
			_pending_samples.clear()
			_read_pos = 0
			_tts_player.stop()
			amplitude_changed.emit(0.0)
			_set_ambient_ducked(false)
			speaking_state_changed.emit(false)


func play_earcon(kind: String) -> void:
	if not earcons_enabled or _earcon_playback == null:
		return
	if not _earcon_player.playing:
		_earcon_player.play()
		_earcon_playback = _earcon_player.get_stream_playback() as AudioStreamGeneratorPlayback
	if _earcon_playback == null:
		return

	var freq_a := 587.33
	var freq_b := 880.0
	var dur_sec := 0.14
	match kind:
		"approval":
			freq_a = 523.25
			freq_b = 659.25
			dur_sec = 0.22
		"notice":
			freq_a = 659.25
			freq_b = 987.77
			dur_sec = 0.22
		"done":
			freq_a = 523.25
			freq_b = 783.99
			dur_sec = 0.18
		_:
			freq_a = 440.0
			freq_b = 554.37
			dur_sec = 0.12

	var sr := 24000.0
	var total := mini(int(sr * dur_sec), _earcon_playback.get_frames_available())
	if total <= 16:
		return
	var half := total / 2
	var frames := PackedVector2Array()
	frames.resize(total)
	for i in range(total):
		var t := float(i) / sr
		var freq := freq_a if i < half else freq_b
		var env := sin(PI * float(i) / float(total)) * 0.16
		var sample := sin(TAU * freq * t) * env
		frames[i] = Vector2(sample, sample)
	_earcon_playback.push_buffer(frames)


func set_ambient_enabled(enabled: bool) -> void:
	_ambient_enabled = enabled
	if enabled:
		if not _ambient_player.playing:
			_ambient_player.play()
			_ambient_playback = _ambient_player.get_stream_playback() as AudioStreamGeneratorPlayback
		_set_ambient_ducked(_is_streaming)
	else:
		if _ambient_player.playing:
			_ambient_player.stop()


func is_ambient_enabled() -> bool:
	return _ambient_enabled


func _pump_ambient_noise() -> void:
	if not _ambient_enabled or _ambient_playback == null:
		return
	var avail := mini(_ambient_playback.get_frames_available(), 1024)
	if avail <= 0:
		return
	var frames := PackedVector2Array()
	frames.resize(avail)
	for i in range(avail):
		# Soft low-passed rain-like brown/pink ambience
		var white := randf_range(-0.22, 0.22)
		_ambient_lpf_state = _ambient_lpf_state * 0.92 + white * 0.08
		frames[i] = Vector2(_ambient_lpf_state, _ambient_lpf_state)
	_ambient_playback.push_buffer(frames)


func _set_ambient_ducked(ducked: bool) -> void:
	var bus_idx := AudioServer.get_bus_index(&"Ambient")
	if bus_idx < 0:
		return
	var target_db := ambient_base_db + (ambient_duck_db if ducked else 0.0)
	if _duck_tween != null and _duck_tween.is_valid():
		_duck_tween.kill()
	_duck_tween = create_tween()
	_duck_tween.tween_method(
		func(db: float) -> void: AudioServer.set_bus_volume_db(bus_idx, db),
		AudioServer.get_bus_volume_db(bus_idx),
		target_db,
		0.25
	)
