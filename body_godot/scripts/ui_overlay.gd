extends Control
class_name QimingxingUIOverlay

signal passthrough_rects_changed(rects: Array[Rect2])
signal approval_decided(approval_id: String, turn_id: String, approved: bool)
signal cancel_requested(turn_id: String)
signal drop_queue_submitted(paths: Array[String])
signal desktop_action_requested(action: String, path: String, media_type: String)
signal text_submitted(text: String)

const PIN_WINDOW_SCENE := preload("res://scenes/PinImageWindow.tscn")
const VIEWPORT_WIDTH := 420.0
const AVATAR_CENTER_X := 210.0
const AVATAR_NECK_Y := 286.0
const BUBBLE_MIN_W := 148.0
const BUBBLE_MAX_W := 296.0
const STACK_WIDTH := 340.0

var _bubble_panel: PanelContainer
var _bubble_label: RichTextLabel
var _bubble_fade_timer: float = 0.0
var _bubble_target_w: float = BUBBLE_MAX_W
var _current_spoken: String = ""
var _current_turn_id: String = ""
var _in_turn: bool = false
var _avatar_scale: float = 0.78

var _stack: VBoxContainer
var _approval_panel: PanelContainer
var _approval_label: Label
var _pending_approval_id: String = ""
var _pending_approval_turn: String = ""

var _drop_panel: PanelContainer
var _drop_label: Label
var _dropped_paths: Array[String] = []

var _artifact_panel: PanelContainer
var _artifact_title: Label
var _artifact_buttons_box: HBoxContainer
var _artifact_path: String = ""
var _artifact_media_type: String = "file"

var _controls_bar: HBoxContainer
var _stop_btn: Button

var _input_panel: PanelContainer
var _input_edit: LineEdit
var _send_btn: Button

signal interaction_active_changed(active: bool)

var _dock_tucked: bool = false


func _ready() -> void:
	_build_ui()
	_emit_rects()


func _make_card_style(bg: Color, border: Color) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.border_color = border
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(12)
	sb.content_margin_left = 10
	sb.content_margin_right = 10
	sb.content_margin_top = 8
	sb.content_margin_bottom = 8
	return sb


func _build_ui() -> void:
	# 1. Transient 1-3 sentence speech bubble anchored right above avatar head
	_bubble_panel = PanelContainer.new()
	_bubble_panel.add_theme_stylebox_override(
		"panel", _make_card_style(Color(0.08, 0.11, 0.17, 0.90), Color(0.28, 0.78, 0.96, 0.65))
	)
	_bubble_panel.position = Vector2((VIEWPORT_WIDTH - BUBBLE_MAX_W) * 0.5, 130.0)
	_bubble_panel.custom_minimum_size = Vector2(BUBBLE_MAX_W, 0.0)
	_bubble_panel.visible = false
	add_child(_bubble_panel)

	_bubble_label = RichTextLabel.new()
	_bubble_label.fit_content = true
	_bubble_label.scroll_active = false
	_bubble_label.bbcode_enabled = false
	_bubble_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_bubble_label.add_theme_font_size_override("normal_font_size", 13)
	_bubble_label.add_theme_color_override("default_color", Color(0.94, 0.97, 1.0))
	_bubble_panel.add_child(_bubble_label)

	# Bottom capsule stack anchored cleanly under scaled avatar ring
	_stack = VBoxContainer.new()
	_stack.custom_minimum_size = Vector2(STACK_WIDTH, 0.0)
	_stack.size = Vector2(STACK_WIDTH, 0.0)
	_stack.add_theme_constant_override("separation", 6)
	_stack.mouse_filter = Control.MOUSE_FILTER_PASS
	add_child(_stack)
	_reposition_stack()

	# 2. Approval / Tool confirmation capsule
	_approval_panel = PanelContainer.new()
	_approval_panel.add_theme_stylebox_override(
		"panel", _make_card_style(Color(0.20, 0.12, 0.06, 0.93), Color(1.0, 0.62, 0.22, 0.85))
	)
	_approval_panel.visible = false
	_stack.add_child(_approval_panel)

	var app_row := HBoxContainer.new()
	app_row.add_theme_constant_override("separation", 6)
	_approval_panel.add_child(app_row)

	_approval_label = Label.new()
	_approval_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_approval_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_approval_label.add_theme_font_size_override("font_size", 12)
	app_row.add_child(_approval_label)

	var allow_btn := Button.new()
	allow_btn.text = "允许"
	allow_btn.pressed.connect(func() -> void: _resolve_approval(true))
	app_row.add_child(allow_btn)

	var deny_btn := Button.new()
	deny_btn.text = "拒绝"
	deny_btn.pressed.connect(func() -> void: _resolve_approval(false))
	app_row.add_child(deny_btn)

	# 3. Drop Queue capsule (files_dropped accumulation)
	_drop_panel = PanelContainer.new()
	_drop_panel.add_theme_stylebox_override(
		"panel", _make_card_style(Color(0.08, 0.17, 0.22, 0.93), Color(0.36, 0.85, 0.98, 0.8))
	)
	_drop_panel.visible = false
	_stack.add_child(_drop_panel)

	var drop_row := HBoxContainer.new()
	drop_row.add_theme_constant_override("separation", 6)
	_drop_panel.add_child(drop_row)

	_drop_label = Label.new()
	_drop_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_drop_label.add_theme_font_size_override("font_size", 12)
	drop_row.add_child(_drop_label)

	var send_drop := Button.new()
	send_drop.text = "投喂挂载"
	send_drop.pressed.connect(_submit_drop_queue)
	drop_row.add_child(send_drop)

	var clr_drop := Button.new()
	clr_drop.text = "清空"
	clr_drop.pressed.connect(clear_drop_queue)
	drop_row.add_child(clr_drop)

	# 4. Artifact native delivery capsule
	_artifact_panel = PanelContainer.new()
	_artifact_panel.add_theme_stylebox_override(
		"panel", _make_card_style(Color(0.09, 0.14, 0.19, 0.93), Color(0.45, 0.85, 0.65, 0.75))
	)
	_artifact_panel.visible = false
	_stack.add_child(_artifact_panel)

	var art_col := VBoxContainer.new()
	art_col.add_theme_constant_override("separation", 4)
	_artifact_panel.add_child(art_col)

	var art_top := HBoxContainer.new()
	art_col.add_child(art_top)

	_artifact_title = Label.new()
	_artifact_title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_artifact_title.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_artifact_title.add_theme_font_size_override("font_size", 12)
	art_top.add_child(_artifact_title)

	var close_art := Button.new()
	close_art.text = "×"
	close_art.pressed.connect(func() -> void:
		_artifact_panel.visible = false
		_emit_rects()
	)
	art_top.add_child(close_art)

	_artifact_buttons_box = HBoxContainer.new()
	_artifact_buttons_box.add_theme_constant_override("separation", 4)
	art_col.add_child(_artifact_buttons_box)

	# 4.5 Inline desktop text input capsule (toggled on click or tray menu)
	_input_panel = PanelContainer.new()
	_input_panel.add_theme_stylebox_override(
		"panel", _make_card_style(Color(0.08, 0.12, 0.19, 0.94), Color(0.32, 0.84, 1.0, 0.80))
	)
	_input_panel.visible = false
	_stack.add_child(_input_panel)

	var input_row := HBoxContainer.new()
	input_row.add_theme_constant_override("separation", 5)
	_input_panel.add_child(input_row)

	_input_edit = LineEdit.new()
	_input_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_input_edit.placeholder_text = "输入想说的话… (Enter发送)"
	_input_edit.add_theme_font_size_override("font_size", 12)
	_input_edit.text_submitted.connect(func(_t: String) -> void: _submit_chat_input())
	_input_edit.gui_input.connect(func(ev: InputEvent) -> void:
		if ev is InputEventKey and ev.pressed and not ev.echo and (ev as InputEventKey).keycode == KEY_ESCAPE:
			set_chat_input_visible(false)
			_input_edit.accept_event()
	)
	input_row.add_child(_input_edit)

	_send_btn = Button.new()
	_send_btn.text = "发送"
	_send_btn.add_theme_font_size_override("font_size", 12)
	_send_btn.pressed.connect(_submit_chat_input)
	input_row.add_child(_send_btn)

	var close_input := Button.new()
	close_input.text = "×"
	close_input.add_theme_font_size_override("font_size", 12)
	close_input.pressed.connect(func() -> void: set_chat_input_visible(false))
	input_row.add_child(close_input)

	# 5. Minimal emergency-stop capsule (hidden cleanly whenever idle)
	_controls_bar = HBoxContainer.new()
	_controls_bar.alignment = BoxContainer.ALIGNMENT_CENTER
	_controls_bar.add_theme_constant_override("separation", 4)
	_controls_bar.visible = false
	_stack.add_child(_controls_bar)

	_stop_btn = Button.new()
	_stop_btn.text = "■ 急停"
	_stop_btn.add_theme_font_size_override("font_size", 11)
	_stop_btn.visible = false
	_stop_btn.pressed.connect(func() -> void: cancel_requested.emit(_current_turn_id))
	_controls_bar.add_child(_stop_btn)


func _process(delta: float) -> void:
	if _bubble_panel.visible and _bubble_fade_timer > 0.0:
		_bubble_fade_timer -= delta
		if _bubble_fade_timer <= 0.0:
			_bubble_panel.visible = false
			queue_redraw()
			_emit_rects()


func on_turn_start(turn_id: String) -> void:
	_current_turn_id = turn_id
	_in_turn = true
	_current_spoken = ""
	_bubble_fade_timer = 0.0
	_bubble_label.text = ""
	_bubble_panel.visible = false
	_stop_btn.visible = true
	_controls_bar.visible = not _dock_tucked
	queue_redraw()
	_emit_rects()


func on_text_delta(delta: String, is_final: bool) -> void:
	if delta != "":
		_current_spoken += delta
		var trimmed := _current_spoken.strip_edges()
		if trimmed.length() > 180:
			trimmed = "…" + trimmed.right(176)
		_show_bubble_text(trimmed, 7.0 if is_final else 0.0)
	elif is_final and _bubble_panel.visible:
		_bubble_fade_timer = 6.5


func on_turn_end() -> void:
	_in_turn = false
	_stop_btn.visible = false
	_controls_bar.visible = false
	if _bubble_panel.visible:
		_bubble_fade_timer = 6.5
	_emit_rects()


func show_transient_status(text: String, fade_sec: float = 4.5) -> void:
	var cleaned := text.strip_edges()
	if cleaned == "":
		return
	_show_bubble_text(cleaned, fade_sec)


func set_avatar_scale(sc: float) -> void:
	_avatar_scale = clampf(sc, 0.50, 1.30)
	_reposition_stack()
	_reposition_bubble()


func _reposition_stack() -> void:
	if _stack == null:
		return
	var stack_x := (VIEWPORT_WIDTH - STACK_WIDTH) * 0.5
	var stack_y := clampf(AVATAR_NECK_Y + 104.0 * _avatar_scale, 332.0, 424.0)
	_stack.position = Vector2(stack_x, stack_y)
	_emit_rects()


func _show_bubble_text(text: String, fade_sec: float) -> void:
	var width := BUBBLE_MAX_W
	if text.find("\n") == -1:
		var font := _bubble_label.get_theme_font("normal_font")
		if font != null:
			var single_w := font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, 13).x
			width = clampf(single_w + 30.0, BUBBLE_MIN_W, BUBBLE_MAX_W)
	_bubble_target_w = width
	_bubble_panel.custom_minimum_size = Vector2(width, 0.0)
	_bubble_panel.size = Vector2(width, 0.0)
	_bubble_label.text = text
	_bubble_panel.visible = true
	_bubble_fade_timer = fade_sec
	_reposition_bubble()
	call_deferred("_reposition_bubble")


func _reposition_bubble() -> void:
	if _bubble_panel == null or not _bubble_panel.visible:
		queue_redraw()
		return
	var min_sz := _bubble_panel.get_combined_minimum_size()
	var w := clampf(maxf(_bubble_target_w, min_sz.x), BUBBLE_MIN_W, BUBBLE_MAX_W)
	var h := maxf(min_sz.y, 32.0)
	_bubble_panel.size = Vector2(w, h)
	var head_top_y := AVATAR_NECK_Y - 130.0 * _avatar_scale
	var x := (VIEWPORT_WIDTH - w) * 0.5
	var y := clampf(head_top_y - h - 12.0, 8.0, 235.0)
	_bubble_panel.position = Vector2(x, y)
	queue_redraw()
	_collect_and_emit_rects()


func _draw() -> void:
	if _bubble_panel == null or not _bubble_panel.visible or _bubble_panel.size.y <= 1.0:
		return
	var r := _bubble_panel.get_rect()
	var base_y := r.position.y + r.size.y - 0.5
	var tail_x := clampf(AVATAR_CENTER_X, r.position.x + 18.0, r.position.x + r.size.x - 18.0)
	var head_top_y := AVATAR_NECK_Y - 130.0 * _avatar_scale
	var tip_y := maxf(base_y + 9.0, minf(base_y + 13.0, head_top_y + 2.0))
	var p_left := Vector2(tail_x - 7.0, base_y)
	var p_right := Vector2(tail_x + 7.0, base_y)
	var p_tip := Vector2(tail_x, tip_y)
	draw_colored_polygon(PackedVector2Array([p_left, p_right, p_tip]), Color(0.08, 0.11, 0.17, 0.90))
	var border_col := Color(0.28, 0.78, 0.96, 0.65)
	draw_line(p_left, p_tip, border_col, 1.2, true)
	draw_line(p_right, p_tip, border_col, 1.2, true)


func show_approval_request(approval_id: String, turn_id: String, summary: String) -> void:
	_pending_approval_id = approval_id
	_pending_approval_turn = turn_id
	_approval_label.text = summary
	_approval_panel.visible = true
	_emit_rects()


func _resolve_approval(approved: bool) -> void:
	_approval_panel.visible = false
	_emit_rects()
	if _pending_approval_id != "":
		approval_decided.emit(_pending_approval_id, _pending_approval_turn, approved)
		_pending_approval_id = ""
		_pending_approval_turn = ""


func queue_dropped_files(files: PackedStringArray) -> void:
	for f in files:
		if not _dropped_paths.has(f):
			_dropped_paths.append(f)
	if _dropped_paths.is_empty():
		return
	_drop_label.text = "待挂载 %d 项文件/目录" % _dropped_paths.size()
	_drop_panel.visible = true
	_emit_rects()


func _submit_drop_queue() -> void:
	if _dropped_paths.is_empty():
		return
	var batch: Array[String] = _dropped_paths.duplicate()
	clear_drop_queue()
	drop_queue_submitted.emit(batch)


func clear_drop_queue() -> void:
	_dropped_paths.clear()
	_drop_panel.visible = false
	_emit_rects()


func show_artifact_capsule(title: String, path: String, media_type: String, actions: Array) -> void:
	_artifact_title.text = "产物: %s" % title
	_artifact_path = path
	_artifact_media_type = media_type
	for ch in _artifact_buttons_box.get_children():
		ch.queue_free()

	var labels := {
		"quick_look": "Quick Look",
		"copy_file": "复制文件",
		"reveal": "Finder",
		"pin_image": "钉在桌面",
		"open_external": "打开",
	}
	for act_var in actions:
		var act := str(act_var)
		var btn := Button.new()
		btn.text = labels.get(act, act)
		btn.pressed.connect(func() -> void:
			if act == "pin_image":
				open_pin_image_window(_artifact_path)
			else:
				desktop_action_requested.emit(act, _artifact_path, _artifact_media_type)
		)
		_artifact_buttons_box.add_child(btn)

	_artifact_panel.visible = true
	_emit_rects()


func open_pin_image_window(abs_path: String) -> void:
	var win := PIN_WINDOW_SCENE.instantiate() as Window
	if win == null:
		return
	get_tree().root.add_child(win)
	win.call("load_image_from_path", abs_path)


func _emit_rects() -> void:
	call_deferred("_collect_and_emit_rects")


func set_dock_tucked(tucked: bool) -> void:
	_dock_tucked = tucked
	if _controls_bar != null:
		_controls_bar.visible = (not tucked) and _in_turn
	if tucked and _input_panel != null:
		_input_panel.visible = false
	_emit_rects()


func set_chat_input_visible(should_show: bool) -> void:
	if _input_panel == null:
		return
	_input_panel.visible = should_show
	if should_show:
		if get_window() != null:
			get_window().grab_focus()
		if _input_edit != null:
			_input_edit.call_deferred("grab_focus")
	_emit_rects()


func toggle_chat_input() -> bool:
	set_chat_input_visible(not is_chat_input_visible())
	return is_chat_input_visible()


func is_chat_input_visible() -> bool:
	return _input_panel != null and _input_panel.visible


func _submit_chat_input() -> void:
	if _input_edit == null:
		return
	var cleaned := _input_edit.text.strip_edges()
	if cleaned == "":
		return
	_input_edit.text = ""
	text_submitted.emit(cleaned)
	_input_edit.call_deferred("grab_focus")


func _collect_and_emit_rects() -> void:
	var rects: Array[Rect2] = []
	for ctrl in [_bubble_panel, _input_panel, _approval_panel, _drop_panel, _artifact_panel, _controls_bar]:
		if ctrl != null and ctrl.visible:
			rects.append(ctrl.get_global_rect())
	var active := (
		_in_turn
		or (_input_panel != null and _input_panel.visible)
		or (_approval_panel != null and _approval_panel.visible)
		or (_drop_panel != null and _drop_panel.visible)
		or (_artifact_panel != null and _artifact_panel.visible)
	)
	interaction_active_changed.emit(active)
	passthrough_rects_changed.emit(rects)
