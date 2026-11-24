extends Window
class_name QimingxingPinImageWindow

@onready var _image_rect: TextureRect = $BackgroundPanel/Margin/ImageRect
@onready var _bg_panel: PanelContainer = $BackgroundPanel

var _dragging: bool = false
var _drag_mouse_start: Vector2i = Vector2i.ZERO
var _drag_win_start: Vector2i = Vector2i.ZERO
var _aspect_ratio: float = 1.38


func _ready() -> void:
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(0.06, 0.08, 0.13, 0.90)
	sb.border_color = Color(0.32, 0.80, 0.98, 0.85)
	sb.set_border_width_all(2)
	sb.set_corner_radius_all(12)
	_bg_panel.add_theme_stylebox_override("panel", sb)
	close_requested.connect(queue_free)


func load_image_from_path(abs_path: String) -> bool:
	if not FileAccess.file_exists(abs_path):
		return false
	var img := Image.load_from_file(abs_path)
	if img == null or img.is_empty():
		return false
	var tex := ImageTexture.create_from_image(img)
	_image_rect.texture = tex
	var iw := float(maxi(1, img.get_width()))
	var ih := float(maxi(1, img.get_height()))
	_aspect_ratio = iw / ih
	var target_w := clampi(int(iw), 240, 540)
	var target_h := clampi(int(round(float(target_w) / _aspect_ratio)) + 26, 180, 520)
	size = Vector2i(target_w, target_h)
	return true


func _input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT:
			if mb.double_click and mb.pressed:
				queue_free()
				return
			if mb.pressed:
				_dragging = true
				_drag_mouse_start = DisplayServer.mouse_get_position()
				_drag_win_start = position
			else:
				_dragging = false
		elif mb.pressed and (mb.button_index == MOUSE_BUTTON_WHEEL_UP or mb.button_index == MOUSE_BUTTON_WHEEL_DOWN):
			var factor := 1.08 if mb.button_index == MOUSE_BUTTON_WHEEL_UP else 0.92
			var nw := clampi(int(round(float(size.x) * factor)), 180, 960)
			var nh := clampi(int(round(float(nw) / _aspect_ratio)) + 24, 140, 860)
			size = Vector2i(nw, nh)
	elif event is InputEventMouseMotion and _dragging:
		var delta := DisplayServer.mouse_get_position() - _drag_mouse_start
		position = _drag_win_start + delta
