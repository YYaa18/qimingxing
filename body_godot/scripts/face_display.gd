extends Node2D

var expression: String = "idle"
var gaze: Vector2 = Vector2.ZERO
var blink_amount: float = 0.0
var is_speaking: bool = false
var rms: float = 0.0
var pulse_time: float = 0.0

const CYAN_GLOW := Color(0.14, 0.86, 1.0, 0.24)
const CYAN_MAIN := Color(0.30, 0.94, 1.0, 0.96)
const CYAN_CORE := Color(0.84, 0.99, 1.0, 0.96)
const VISOR_DARK := Color(0.08, 0.14, 0.22, 0.98)


func _process(delta: float) -> void:
	pulse_time += delta
	queue_redraw()


func _draw() -> void:
	var eye_shift := Vector2(gaze.x * 7.5, gaze.y * 4.2)
	var eye_y := -1.5 + eye_shift.y
	var left_c := Vector2(-17.0 + eye_shift.x, eye_y)
	var right_c := Vector2(17.0 + eye_shift.x, eye_y)

	# Warm pink cheeks for happy / playful state
	if expression == "happy":
		_draw_blush(left_c + Vector2(-3.0, 11.5))
		_draw_blush(right_c + Vector2(3.0, 11.5))

	# Draw expressive eyes (with smooth blink squish)
	if blink_amount >= 0.82:
		_draw_closed_blink_eye(left_c)
		_draw_closed_blink_eye(right_c)
	else:
		var open_scale := clampf(1.0 - blink_amount, 0.15, 1.0)
		match expression:
			"happy":
				_draw_happy_arc(left_c, open_scale)
				_draw_happy_arc(right_c, open_scale)
			"thinking":
				_draw_solid_orb_eye(left_c, open_scale, 7.2)
				_draw_solid_orb_eye(right_c, open_scale, 7.2)
			"working":
				_draw_focused_eye(left_c, true, open_scale)
				_draw_focused_eye(right_c, false, open_scale)
			"alert":
				_draw_ring_eye(left_c, open_scale, 9.2, false)
				_draw_ring_eye(right_c, open_scale, 8.6, false)
			"sorry":
				_draw_sad_eye(left_c, true, open_scale)
				_draw_sad_eye(right_c, false, open_scale)
			"receiving":
				_draw_ring_eye(left_c, open_scale, 9.5, true)
				_draw_ring_eye(right_c, open_scale, 9.5, true)
			_:
				_draw_happy_arc(left_c, open_scale)
				_draw_happy_arc(right_c, open_scale)

	# Real-time reactive cyan voice waveform on visor when speaking
	if is_speaking and rms > 0.025:
		_draw_voice_waveform(Vector2(eye_shift.x * 0.35, 16.5))


func _draw_neon_polyline(pts: PackedVector2Array, width: float = 3.6) -> void:
	draw_polyline(pts, CYAN_GLOW, width + 4.2, true)
	draw_polyline(pts, CYAN_MAIN, width, true)
	draw_polyline(pts, CYAN_CORE, maxf(1.2, width * 0.42), true)


func _draw_closed_blink_eye(c: Vector2) -> void:
	var pts := PackedVector2Array([c + Vector2(-8.0, 1.5), c + Vector2(8.0, 1.5)])
	_draw_neon_polyline(pts, 3.0)


func _draw_happy_arc(c: Vector2, open_scale: float) -> void:
	var pts := PackedVector2Array()
	var segs := 14
	for i in range(segs + 1):
		var t := float(i) / float(segs)
		var x := lerpf(-8.5, 8.5, t)
		var curve := 1.0 - pow(2.0 * t - 1.0, 2.0)
		var y := (3.0 - curve * 7.8) * open_scale
		pts.append(c + Vector2(x, y))
	_draw_neon_polyline(pts, 3.8)


func _draw_solid_orb_eye(c: Vector2, open_scale: float, radius: float) -> void:
	var ry := radius * open_scale
	var rx := radius * 1.06
	_draw_ellipse(c, rx + 2.4, ry + 2.4, CYAN_GLOW)
	_draw_ellipse(c, rx, ry, CYAN_MAIN)
	_draw_ellipse(c - Vector2(1.5, 1.5 * open_scale), rx * 0.42, ry * 0.42, CYAN_CORE)


func _draw_ring_eye(c: Vector2, open_scale: float, radius: float, sparkle: bool) -> void:
	var rx := radius
	var ry := radius * open_scale
	_draw_ellipse(c, rx + 2.5, ry + 2.5, CYAN_GLOW)
	_draw_ellipse(c, rx, ry, CYAN_MAIN)
	_draw_ellipse(c, rx * 0.62, ry * 0.62, VISOR_DARK)
	if sparkle and open_scale > 0.5:
		draw_circle(c + Vector2(-2.6, -2.8 * open_scale), 2.1, CYAN_CORE)
		draw_circle(c + Vector2(2.2, 2.0 * open_scale), 1.1, CYAN_MAIN)


func _draw_focused_eye(c: Vector2, is_left: bool, open_scale: float) -> void:
	var dir := 1.0 if is_left else -1.0
	var pts := PackedVector2Array([
		c + Vector2(-7.5 * dir, -2.8 * open_scale),
		c + Vector2(7.5 * dir, 1.5 * open_scale),
		c + Vector2(-4.5 * dir, 3.8 * open_scale),
	])
	_draw_neon_polyline(pts, 3.4)


func _draw_sad_eye(c: Vector2, is_left: bool, open_scale: float) -> void:
	var dir := 1.0 if is_left else -1.0
	var pts := PackedVector2Array([
		c + Vector2(-7.5 * dir, 3.0 * open_scale),
		c + Vector2(0.0, -3.5 * open_scale),
		c + Vector2(7.5 * dir, -1.5 * open_scale),
	])
	_draw_neon_polyline(pts, 3.4)


func _draw_blush(c: Vector2) -> void:
	_draw_ellipse(c, 9.5, 5.2, Color(0.98, 0.46, 0.42, 0.22))
	_draw_ellipse(c, 6.0, 3.2, Color(1.00, 0.52, 0.46, 0.34))


func _draw_ellipse(center: Vector2, rx: float, ry: float, col: Color) -> void:
	var pts := PackedVector2Array()
	var segs := 20
	for i in range(segs):
		var ang := TAU * float(i) / float(segs)
		pts.append(center + Vector2(cos(ang) * rx, sin(ang) * ry))
	draw_colored_polygon(pts, col)


func _draw_voice_waveform(center: Vector2) -> void:
	var bars := 11
	var half := bars / 2
	var spacing := 3.6
	for idx in range(bars):
		var dist := float(idx - half) / float(half)
		var env := cos(dist * PI * 0.46)
		var osc := 0.55 + 0.45 * absf(sin(pulse_time * 16.0 + float(idx) * 0.85))
		var h := clampf(1.5 + rms * 13.5 * env * osc, 1.5, 11.5)
		var x := center.x + float(idx - half) * spacing
		var p0 := Vector2(x, center.y - h * 0.5)
		var p1 := Vector2(x, center.y + h * 0.5)
		draw_line(p0, p1, CYAN_GLOW, 3.8, true)
		draw_line(p0, p1, CYAN_MAIN, 1.8, true)
		draw_line(p0, p1, CYAN_CORE, 0.9, true)
