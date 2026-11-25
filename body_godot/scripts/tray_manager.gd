extends Node

signal open_chat_input_requested
signal open_settings_requested
signal open_shortcuts_requested
signal open_mcp_config_requested
signal toggle_roam_requested(enabled: bool)
signal toggle_earcons_requested(enabled: bool)
signal toggle_ambient_requested(enabled: bool)
signal reset_position_requested
signal reload_mcp_requested
signal open_web_requested
signal quit_requested

const TRAY_POPOVER_SCENE := preload("res://scenes/TrayPopoverWindow.tscn")
const UITokens := preload("res://theme/ui_tokens.gd")
const TRAY_ICON_PATH := "res://assets/tray_icon_template.png"

const ID_SETTINGS := 1
const ID_ROAM := 2
const ID_EARCONS := 3
const ID_AMBIENT := 4
const ID_RESET_POS := 5
const ID_RELOAD_MCP := 6
const ID_OPEN_WEB := 7
const ID_CHAT_INPUT := 8
const ID_SHORTCUTS := 9
const ID_MCP_CONFIG := 10
const ID_QUIT := 99

var _indicator: StatusIndicator
var _menu: PopupMenu
var _popover: Window
var _cached_roam: bool = true
var _cached_earcons: bool = true
var _cached_ambient: bool = false
var _cached_connected: bool = true
var _cached_profile: String = ""


func _ready() -> void:
	_menu = PopupMenu.new()
	_menu.name = "TrayMenu"
	_menu.theme = UITokens.build_obsidian_theme()
	add_child(_menu)

	_menu.add_item("文字对话输入框", ID_CHAT_INPUT)
	_menu.add_item("设置与 API Key…", ID_SETTINGS)
	_menu.add_item("快捷键管理…", ID_SHORTCUTS)
	_menu.add_item("MCP 服务配置…", ID_MCP_CONFIG)
	_menu.add_separator()
	_menu.add_check_item("屏幕自由漫步", ID_ROAM)
	_menu.add_check_item("状态微音效", ID_EARCONS)
	_menu.add_check_item("背景氛围音", ID_AMBIENT)
	_menu.add_separator()
	_menu.add_item("重置位置 / 取消贴边", ID_RESET_POS)
	_menu.add_item("热重载 MCP 工具", ID_RELOAD_MCP)
	_menu.add_item("打开 Web 调试控制台", ID_OPEN_WEB)
	_menu.add_separator()
	_menu.add_item("退出桌面真身", ID_QUIT)

	_menu.set_item_checked(_menu.get_item_index(ID_ROAM), true)
	_menu.set_item_checked(_menu.get_item_index(ID_EARCONS), true)
	_menu.set_item_checked(_menu.get_item_index(ID_AMBIENT), false)
	_menu.id_pressed.connect(_on_menu_id_pressed)

	if ClassDB.class_exists(&"StatusIndicator"):
		_indicator = StatusIndicator.new()
		_indicator.name = "QimingxingTray"
		_indicator.tooltip = "启明星（Qimingxing）桌面数字人"
		var icon_tex := _load_tray_icon()
		if icon_tex != null:
			_indicator.icon = icon_tex
		_indicator.visible = true
		add_child(_indicator)
		# Keep _indicator.menu unset so StatusIndicator.pressed handles left-click popover & right-click fallback menu
		_indicator.pressed.connect(_on_indicator_pressed)

	call_deferred("_ensure_popover")


func _ensure_popover() -> Window:
	if _popover == null:
		_popover = TRAY_POPOVER_SCENE.instantiate() as Window
		get_tree().root.add_child(_popover)
		_popover.action_triggered.connect(_on_menu_id_pressed)
		_popover.toggle_changed.connect(_on_popover_toggle_changed)
		_popover.call("sync_toggles", _cached_roam, _cached_earcons, _cached_ambient)
		_popover.call("sync_status", _cached_connected, _cached_profile)
	return _popover


func _load_tray_icon() -> Texture2D:
	var abs_path := ProjectSettings.globalize_path(TRAY_ICON_PATH)
	if FileAccess.file_exists(abs_path):
		var img := Image.load_from_file(abs_path)
		if img != null and not img.is_empty():
			return ImageTexture.create_from_image(img)
	if ResourceLoader.exists(TRAY_ICON_PATH):
		var res := load(TRAY_ICON_PATH)
		if res is Texture2D:
			return res
	return null


func _on_indicator_pressed(mouse_button: int, _mouse_position: Vector2i) -> void:
	if mouse_button == MOUSE_BUTTON_RIGHT:
		if _popover != null and _popover.visible:
			_popover.call("hide_popover")
		popup_at_cursor()
		return
	var pop := _ensure_popover()
	if pop.visible:
		pop.call("hide_popover")
	else:
		var rect := _indicator.get_rect() if _indicator != null else Rect2()
		pop.call("open_near_rect", rect)


func open_popover() -> void:
	var pop := _ensure_popover()
	var rect := _indicator.get_rect() if _indicator != null else Rect2()
	pop.call("open_near_rect", rect)


func popup_at_cursor() -> void:
	var pop := _ensure_popover()
	if pop.visible:
		pop.call("hide_popover")
	else:
		pop.call("open_at_cursor")


func sync_toggles(roam_on: bool, earcons_on: bool, ambient_on: bool) -> void:
	_cached_roam = roam_on
	_cached_earcons = earcons_on
	_cached_ambient = ambient_on
	if _menu != null:
		_menu.set_item_checked(_menu.get_item_index(ID_ROAM), roam_on)
		_menu.set_item_checked(_menu.get_item_index(ID_EARCONS), earcons_on)
		_menu.set_item_checked(_menu.get_item_index(ID_AMBIENT), ambient_on)
	if _popover != null:
		_popover.call("sync_toggles", roam_on, earcons_on, ambient_on)


func sync_status(connected: bool, active_profile: String) -> void:
	_cached_connected = connected
	_cached_profile = active_profile
	if _popover != null:
		_popover.call("sync_status", connected, active_profile)


func sync_shortcut_hints(chat_hk: String, ptt_hk: String, settings_hk: String = "") -> void:
	var chat_clean := chat_hk.strip_edges()
	var ptt_clean := ptt_hk.strip_edges()
	if _menu != null:
		var idx := _menu.get_item_index(ID_CHAT_INPUT)
		if idx >= 0:
			if chat_clean != "" and not (chat_clean.to_lower() in ["disabled", "off", "none"]):
				_menu.set_item_text(idx, "文字对话输入框 (%s)" % UITokens.format_shortcut(chat_clean))
			else:
				_menu.set_item_text(idx, "文字对话输入框")
	if _popover != null:
		_popover.call("sync_shortcut_hints", chat_clean, settings_hk)
	if _indicator != null:
		var tip := "启明星（Qimingxing）桌面数字人"
		var parts: Array[String] = []
		if chat_clean != "" and not (chat_clean.to_lower() in ["disabled", "off", "none"]):
			parts.append("文字: %s" % chat_clean)
		if ptt_clean != "" and not (ptt_clean.to_lower() in ["disabled", "off", "none"]):
			parts.append("语音: %s" % ptt_clean)
		if not parts.is_empty():
			tip += "\n快捷键: " + " | ".join(parts)
		_indicator.tooltip = tip


func _on_popover_toggle_changed(action_id: int, enabled: bool) -> void:
	match action_id:
		ID_ROAM:
			_cached_roam = enabled
			if _menu != null:
				_menu.set_item_checked(_menu.get_item_index(ID_ROAM), enabled)
			toggle_roam_requested.emit(enabled)
		ID_EARCONS:
			_cached_earcons = enabled
			if _menu != null:
				_menu.set_item_checked(_menu.get_item_index(ID_EARCONS), enabled)
			toggle_earcons_requested.emit(enabled)
		ID_AMBIENT:
			_cached_ambient = enabled
			if _menu != null:
				_menu.set_item_checked(_menu.get_item_index(ID_AMBIENT), enabled)
			toggle_ambient_requested.emit(enabled)


func _on_menu_id_pressed(id: int) -> void:
	match id:
		ID_CHAT_INPUT:
			open_chat_input_requested.emit()
		ID_SETTINGS:
			open_settings_requested.emit()
		ID_SHORTCUTS:
			open_shortcuts_requested.emit()
		ID_MCP_CONFIG:
			open_mcp_config_requested.emit()
		ID_ROAM:
			var idx := _menu.get_item_index(ID_ROAM)
			var next_val := not _menu.is_item_checked(idx)
			_on_popover_toggle_changed(ID_ROAM, next_val)
			if _popover != null:
				_popover.call("sync_toggles", _cached_roam, _cached_earcons, _cached_ambient)
		ID_EARCONS:
			var idx := _menu.get_item_index(ID_EARCONS)
			var next_val := not _menu.is_item_checked(idx)
			_on_popover_toggle_changed(ID_EARCONS, next_val)
			if _popover != null:
				_popover.call("sync_toggles", _cached_roam, _cached_earcons, _cached_ambient)
		ID_AMBIENT:
			var idx := _menu.get_item_index(ID_AMBIENT)
			var next_val := not _menu.is_item_checked(idx)
			_on_popover_toggle_changed(ID_AMBIENT, next_val)
			if _popover != null:
				_popover.call("sync_toggles", _cached_roam, _cached_earcons, _cached_ambient)
		ID_RESET_POS:
			reset_position_requested.emit()
		ID_RELOAD_MCP:
			reload_mcp_requested.emit()
		ID_OPEN_WEB:
			open_web_requested.emit()
		ID_QUIT:
			quit_requested.emit()
