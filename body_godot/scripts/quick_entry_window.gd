extends Window

signal prompt_submitted(text: String, with_screen: bool, attached_paths: Array[String])
signal profile_selected(profile_name: String)
signal new_chat_requested
signal visibility_changed_custom(is_visible: bool)

var _card: PanelContainer
var _prompt_edit: TextEdit
var _chip_row: HBoxContainer
var _chip_label: Label
var _file_btn: Button
var _screen_btn: Button
var _new_chat_btn: Button
var _profile_opt: OptionButton
var _send_btn: Button
var _file_dialog: FileDialog

var _with_screen: bool = false
var _attached_paths: Array[String] = []
var _profile_names: Array[String] = []
var _is_syncing_profiles: bool = false

var _dragging: bool = false
var _drag_mouse_start: Vector2i
var _drag_win_start: Vector2i

var _pill_normal: StyleBoxFlat
var _pill_hover: StyleBoxFlat
var _pill_active: StyleBoxFlat
var _send_ready_style: StyleBoxFlat
var _send_muted_style: StyleBoxFlat


func _ready() -> void:
	_init_styles()
	_build_ui()
	close_requested.connect(close_quick_entry)
	files_dropped.connect(_on_files_dropped)


func _init_styles() -> void:
	_pill_normal = _make_pill_style(Color(0.16, 0.18, 0.22, 0.92), Color(1.0, 1.0, 1.0, 0.08), 8)
	_pill_hover = _make_pill_style(Color(0.22, 0.25, 0.30, 0.96), Color(1.0, 1.0, 1.0, 0.18), 8)
	_pill_active = _make_pill_style(Color(0.16, 0.46, 0.62, 0.95), Color(0.38, 0.84, 1.0, 0.70), 8)
	_send_ready_style = _make_pill_style(Color(0.22, 0.74, 0.97, 1.0), Color(0.45, 0.88, 1.0, 0.9), 10)
	_send_muted_style = _make_pill_style(Color(0.20, 0.22, 0.27, 0.85), Color(1.0, 1.0, 1.0, 0.06), 10)


func _make_pill_style(bg: Color, border: Color, radius: int, pad_x: int = 10, pad_y: int = 5) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.border_color = border
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(radius)
	sb.content_margin_left = pad_x
	sb.content_margin_right = pad_x
	sb.content_margin_top = pad_y
	sb.content_margin_bottom = pad_y
	return sb


func _style_pill_button(btn: Button, active: bool = false) -> void:
	var base := _pill_active if active else _pill_normal
	btn.add_theme_stylebox_override("normal", base)
	btn.add_theme_stylebox_override("hover", base if active else _pill_hover)
	btn.add_theme_stylebox_override("pressed", _pill_active)
	btn.add_theme_stylebox_override("focus", base)
	var col := Color(0.96, 0.99, 1.0) if active else Color(0.84, 0.88, 0.94)
	btn.add_theme_color_override("font_color", col)
	btn.add_theme_color_override("font_hover_color", Color(0.98, 0.99, 1.0))
	btn.add_theme_color_override("font_pressed_color", Color(1.0, 1.0, 1.0))


func _build_ui() -> void:
	var outer := MarginContainer.new()
	outer.set_anchors_preset(Control.PRESET_FULL_RECT)
	outer.add_theme_constant_override("margin_left", 14)
	outer.add_theme_constant_override("margin_right", 14)
	outer.add_theme_constant_override("margin_top", 12)
	outer.add_theme_constant_override("margin_bottom", 16)
	add_child(outer)

	_card = PanelContainer.new()
	var card_sb := StyleBoxFlat.new()
	card_sb.bg_color = Color(0.11, 0.12, 0.15, 0.96)
	card_sb.border_color = Color(1.0, 1.0, 1.0, 0.14)
	card_sb.set_border_width_all(1)
	card_sb.set_corner_radius_all(16)
	card_sb.shadow_size = 12
	card_sb.shadow_offset = Vector2(0, 5)
	card_sb.shadow_color = Color(0.0, 0.0, 0.0, 0.45)
	card_sb.content_margin_left = 16
	card_sb.content_margin_right = 16
	card_sb.content_margin_top = 14
	card_sb.content_margin_bottom = 12
	_card.add_theme_stylebox_override("panel", card_sb)
	_card.gui_input.connect(_on_card_gui_input)
	outer.add_child(_card)

	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", 10)
	_card.add_child(col)

	# 1. Top composer row (Claude emblem + borderless TextEdit + subtle close)
	var top_row := HBoxContainer.new()
	top_row.add_theme_constant_override("separation", 10)
	col.add_child(top_row)

	var emblem := Label.new()
	emblem.text = "✦"
	emblem.vertical_alignment = VERTICAL_ALIGNMENT_TOP
	emblem.add_theme_font_size_override("font_size", 20)
	emblem.add_theme_color_override("font_color", Color(0.38, 0.84, 1.0))
	top_row.add_child(emblem)

	_prompt_edit = TextEdit.new()
	_prompt_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_prompt_edit.custom_minimum_size = Vector2(0, 72)
	_prompt_edit.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	_prompt_edit.scroll_fit_content_height = false
	_prompt_edit.placeholder_text = "问启明星任何问题…（Enter 发送 · Shift+Enter 换行 · Esc 关闭）"
	_prompt_edit.add_theme_stylebox_override("normal", StyleBoxEmpty.new())
	_prompt_edit.add_theme_stylebox_override("focus", StyleBoxEmpty.new())
	_prompt_edit.add_theme_font_size_override("font_size", 16)
	_prompt_edit.add_theme_color_override("font_color", Color(0.95, 0.97, 1.0))
	_prompt_edit.add_theme_color_override("font_placeholder_color", Color(0.55, 0.60, 0.68))
	_prompt_edit.add_theme_color_override("caret_color", Color(0.38, 0.84, 1.0))
	_prompt_edit.text_changed.connect(_refresh_send_button_state)
	_prompt_edit.gui_input.connect(_on_prompt_gui_input)
	top_row.add_child(_prompt_edit)

	var close_btn := Button.new()
	close_btn.text = "×"
	close_btn.size_flags_vertical = Control.SIZE_SHRINK_BEGIN
	close_btn.add_theme_stylebox_override("normal", StyleBoxEmpty.new())
	close_btn.add_theme_stylebox_override("hover", _pill_hover)
	close_btn.add_theme_stylebox_override("pressed", _pill_hover)
	close_btn.add_theme_stylebox_override("focus", StyleBoxEmpty.new())
	close_btn.add_theme_font_size_override("font_size", 15)
	close_btn.add_theme_color_override("font_color", Color(0.55, 0.60, 0.68))
	close_btn.add_theme_color_override("font_hover_color", Color(0.94, 0.96, 1.0))
	close_btn.pressed.connect(close_quick_entry)
	top_row.add_child(close_btn)

	# 2. Optional attachment / screen-capture context chip row
	_chip_row = HBoxContainer.new()
	_chip_row.add_theme_constant_override("separation", 8)
	_chip_row.visible = false
	col.add_child(_chip_row)

	var chip_card := PanelContainer.new()
	chip_card.add_theme_stylebox_override(
		"panel", _make_pill_style(Color(0.14, 0.24, 0.34, 0.92), Color(0.35, 0.82, 0.98, 0.45), 8, 10, 4)
	)
	_chip_row.add_child(chip_card)

	_chip_label = Label.new()
	_chip_label.add_theme_font_size_override("font_size", 12)
	_chip_label.add_theme_color_override("font_color", Color(0.84, 0.95, 1.0))
	chip_card.add_child(_chip_label)

	var clr_chip_btn := Button.new()
	clr_chip_btn.text = "清空"
	clr_chip_btn.add_theme_font_size_override("font_size", 11)
	_style_pill_button(clr_chip_btn, false)
	clr_chip_btn.pressed.connect(_clear_attachments)
	_chip_row.add_child(clr_chip_btn)

	# 3. Hairline divider
	var divider := ColorRect.new()
	divider.custom_minimum_size = Vector2(0, 1)
	divider.color = Color(1.0, 1.0, 1.0, 0.08)
	col.add_child(divider)

	# 4. Bottom toolbar (attachments + look screen + new chat | spacer | model + submit)
	var bot_row := HBoxContainer.new()
	bot_row.add_theme_constant_override("separation", 8)
	col.add_child(bot_row)

	_file_btn = Button.new()
	_file_btn.text = "+ 文件"
	_file_btn.add_theme_font_size_override("font_size", 12)
	_style_pill_button(_file_btn, false)
	_file_btn.pressed.connect(_open_file_picker)
	bot_row.add_child(_file_btn)

	_screen_btn = Button.new()
	_screen_btn.text = "📷 看屏"
	_screen_btn.toggle_mode = true
	_screen_btn.add_theme_font_size_override("font_size", 12)
	_style_pill_button(_screen_btn, false)
	_screen_btn.toggled.connect(_on_screen_toggled)
	bot_row.add_child(_screen_btn)

	_new_chat_btn = Button.new()
	_new_chat_btn.text = "↻ 新对话"
	_new_chat_btn.add_theme_font_size_override("font_size", 12)
	_style_pill_button(_new_chat_btn, false)
	_new_chat_btn.pressed.connect(_on_new_chat_pressed)
	bot_row.add_child(_new_chat_btn)

	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bot_row.add_child(spacer)

	_profile_opt = OptionButton.new()
	_profile_opt.add_theme_font_size_override("font_size", 12)
	_profile_opt.add_theme_stylebox_override("normal", _pill_normal)
	_profile_opt.add_theme_stylebox_override("hover", _pill_hover)
	_profile_opt.add_theme_stylebox_override("pressed", _pill_hover)
	_profile_opt.add_theme_stylebox_override("focus", _pill_normal)
	_profile_opt.add_theme_color_override("font_color", Color(0.84, 0.88, 0.94))
	_profile_opt.item_selected.connect(_on_profile_item_selected)
	bot_row.add_child(_profile_opt)

	_send_btn = Button.new()
	_send_btn.text = "↑"
	_send_btn.custom_minimum_size = Vector2(34, 30)
	_send_btn.add_theme_font_size_override("font_size", 15)
	_send_btn.pressed.connect(_submit_prompt)
	bot_row.add_child(_send_btn)

	# Native multi-file picker for + 文件
	_file_dialog = FileDialog.new()
	_file_dialog.file_mode = FileDialog.FILE_MODE_OPEN_FILES
	_file_dialog.access = FileDialog.ACCESS_FILESYSTEM
	_file_dialog.use_native_dialog = true
	_file_dialog.title = "选择挂载给启明星的文件"
	_file_dialog.files_selected.connect(_on_dialog_files_selected)
	add_child(_file_dialog)

	_refresh_chips()
	_refresh_send_button_state()


func open_quick_entry() -> void:
	var usable := DisplayServer.screen_get_usable_rect()
	var win_w := size.x
	var target_x := usable.position.x + int((usable.size.x - win_w) * 0.5)
	var target_y := usable.position.y + int(usable.size.y * 0.22)
	position = Vector2i(target_x, target_y)
	visible = true
	visibility_changed_custom.emit(true)
	grab_focus()
	if _prompt_edit != null:
		_prompt_edit.call_deferred("grab_focus")
	call_deferred("_sync_window_height")


func close_quick_entry() -> void:
	if visible:
		visible = false
		visibility_changed_custom.emit(false)


func toggle_quick_entry() -> bool:
	if visible:
		close_quick_entry()
	else:
		open_quick_entry()
	return visible


func sync_profiles(active_profile: String, profiles: Array) -> void:
	if _profile_opt == null:
		return
	_is_syncing_profiles = true
	_profile_opt.clear()
	_profile_names.clear()
	var sel_idx := 0
	for i in range(profiles.size()):
		var item: Variant = profiles[i]
		if typeof(item) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = item
		var pname := str(d.get("name", ""))
		var label := str(d.get("label", pname))
		if pname == "":
			continue
		var idx := _profile_names.size()
		_profile_names.append(pname)
		_profile_opt.add_item(label, idx)
		if pname == active_profile:
			sel_idx = idx
	if not _profile_names.is_empty():
		_profile_opt.select(sel_idx)
	_is_syncing_profiles = false


func _on_prompt_gui_input(ev: InputEvent) -> void:
	if not (ev is InputEventKey):
		return
	var kev := ev as InputEventKey
	if not kev.pressed or kev.echo:
		return
	if kev.keycode == KEY_ESCAPE:
		close_quick_entry()
		_prompt_edit.accept_event()
		return
	if (
		(kev.keycode == KEY_ENTER or kev.keycode == KEY_KP_ENTER)
		and not kev.shift_pressed
		and not kev.ctrl_pressed
		and not kev.alt_pressed
		and not kev.meta_pressed
	):
		_submit_prompt()
		_prompt_edit.accept_event()


func _on_card_gui_input(ev: InputEvent) -> void:
	if ev is InputEventMouseButton:
		var mb := ev as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT:
			if mb.pressed:
				_dragging = true
				_drag_mouse_start = DisplayServer.mouse_get_position()
				_drag_win_start = position
			else:
				_dragging = false
	elif ev is InputEventMouseMotion and _dragging:
		var cur := DisplayServer.mouse_get_position()
		position = _drag_win_start + (cur - _drag_mouse_start)


func _on_screen_toggled(pressed: bool) -> void:
	_with_screen = pressed
	_style_pill_button(_screen_btn, pressed)
	_refresh_chips()
	_refresh_send_button_state()


func _open_file_picker() -> void:
	if _file_dialog != null:
		_file_dialog.popup_centered_ratio(0.65)


func _on_dialog_files_selected(paths: PackedStringArray) -> void:
	_add_paths(paths)


func _on_files_dropped(files: PackedStringArray) -> void:
	_add_paths(files)


func _add_paths(files: PackedStringArray) -> void:
	for p in files:
		var s := str(p)
		if s != "" and not _attached_paths.has(s):
			_attached_paths.append(s)
	_refresh_chips()
	_refresh_send_button_state()
	if _prompt_edit != null:
		_prompt_edit.call_deferred("grab_focus")


func _clear_attachments() -> void:
	_with_screen = false
	if _screen_btn != null:
		_screen_btn.set_pressed_no_signal(false)
		_style_pill_button(_screen_btn, false)
	_attached_paths.clear()
	_refresh_chips()
	_refresh_send_button_state()


func _refresh_chips() -> void:
	if _chip_row == null or _chip_label == null:
		return
	var parts: Array[String] = []
	if _with_screen:
		parts.append("📷 将附带当前屏幕画面")
	if not _attached_paths.is_empty():
		parts.append("📎 已挂载 %d 项文件" % _attached_paths.size())
	_chip_row.visible = not parts.is_empty()
	_chip_label.text = "   ".join(parts)
	call_deferred("_sync_window_height")


func _sync_window_height() -> void:
	if _card == null:
		return
	var target_h := int(ceil(_card.get_combined_minimum_size().y)) + 28
	target_h = clampi(target_h, 178, 300)
	if size.y != target_h:
		size = Vector2i(size.x, target_h)


func _refresh_send_button_state() -> void:
	if _send_btn == null or _prompt_edit == null:
		return
	var has_content := (
		_prompt_edit.text.strip_edges() != ""
		or _with_screen
		or not _attached_paths.is_empty()
	)
	var st := _send_ready_style if has_content else _send_muted_style
	_send_btn.add_theme_stylebox_override("normal", st)
	_send_btn.add_theme_stylebox_override("hover", _send_ready_style if has_content else _pill_hover)
	_send_btn.add_theme_stylebox_override("pressed", _send_ready_style)
	_send_btn.add_theme_stylebox_override("focus", st)
	_send_btn.add_theme_color_override(
		"font_color", Color(0.06, 0.10, 0.14) if has_content else Color(0.58, 0.62, 0.68)
	)
	_send_btn.disabled = not has_content


func _on_profile_item_selected(idx: int) -> void:
	if _is_syncing_profiles or idx < 0 or idx >= _profile_names.size():
		return
	profile_selected.emit(_profile_names[idx])


func _on_new_chat_pressed() -> void:
	new_chat_requested.emit()
	_prompt_edit.placeholder_text = "已开启新对话，输入你的新问题…"
	if _prompt_edit != null:
		_prompt_edit.call_deferred("grab_focus")


func _submit_prompt() -> void:
	if _prompt_edit == null:
		return
	var text := _prompt_edit.text.strip_edges()
	if text == "" and not _with_screen and _attached_paths.is_empty():
		return
	var use_screen := _with_screen
	var paths: Array[String] = _attached_paths.duplicate()
	_prompt_edit.text = ""
	_clear_attachments()
	close_quick_entry()
	prompt_submitted.emit(text, use_screen, paths)
