extends Control

signal toggled(is_on: bool)

const UITokens := preload("res://theme/ui_tokens.gd")

@export var checked: bool = false

const TRACK_W := 38.0
const TRACK_H := 20.0
const KNOB_RADIUS := 8.0
const KNOB_MARGIN := 2.0

var _knob_t: float = 0.0
var _hovered: bool = false
var _tween: Tween


func _init() -> void:
	custom_minimum_size = Vector2(TRACK_W, TRACK_H)
	size_flags_vertical = Control.SIZE_SHRINK_CENTER
	mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	mouse_filter = Control.MOUSE_FILTER_STOP


func _ready() -> void:
	_knob_t = 1.0 if checked else 0.0
	mouse_entered.connect(func() -> void:
		_hovered = true
		queue_redraw()
	)
	mouse_exited.connect(func() -> void:
		_hovered = false
		queue_redraw()
	)
	queue_redraw()


func is_checked() -> bool:
	return checked


func set_checked(value: bool, animate: bool = true) -> void:
	checked = value
	var target := 1.0 if checked else 0.0
	if _tween != null and _tween.is_running():
		_tween.kill()
	if not animate or not is_inside_tree():
		_knob_t = target
		queue_redraw()
		return
	_tween = create_tween()
	_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_tween.tween_method(_set_knob_t, _knob_t, target, 0.15)


func _set_knob_t(val: float) -> void:
	_knob_t = val
	queue_redraw()


func _gui_input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT and mb.pressed:
			accept_event()
			set_checked(not checked, true)
			toggled.emit(checked)


func _draw() -> void:
	var ox := maxf(0.0, (size.x - TRACK_W) * 0.5)
	var oy := maxf(0.0, (size.y - TRACK_H) * 0.5)
	var track_rect := Rect2(Vector2(ox, oy), Vector2(TRACK_W, TRACK_H))
	var radius := TRACK_H * 0.5

	var off_col := UITokens.SWITCH_OFF
	var on_col := UITokens.ACCENT_CYAN_HOVER if _hovered else UITokens.ACCENT_CYAN
	if _hovered and not checked:
		off_col = off_col.lightened(0.08)
	var track_col := off_col.lerp(on_col, _knob_t)

	var sb := StyleBoxFlat.new()
	sb.bg_color = track_col
	sb.set_corner_radius_all(int(radius))
	sb.border_color = Color(1.0, 1.0, 1.0, 0.12)
	sb.set_border_width_all(1)
	sb.anti_aliasing = true
	draw_style_box(sb, track_rect)

	var min_x := ox + KNOB_MARGIN + KNOB_RADIUS
	var max_x := ox + TRACK_W - KNOB_MARGIN - KNOB_RADIUS
	var knob_x := lerpf(min_x, max_x, _knob_t)
	var knob_y := oy + TRACK_H * 0.5

	draw_circle(Vector2(knob_x, knob_y + 1.0), KNOB_RADIUS, Color(0.0, 0.0, 0.0, 0.28))
	draw_circle(Vector2(knob_x, knob_y), KNOB_RADIUS, Color.WHITE)
