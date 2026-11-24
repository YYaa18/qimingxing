extends Node

signal open_settings_requested
signal toggle_roam_requested(enabled: bool)
signal toggle_earcons_requested(enabled: bool)
signal toggle_ambient_requested(enabled: bool)
signal reset_position_requested
signal reload_mcp_requested
signal open_web_requested
signal quit_requested

const TRAY_ICON_PATH := "res://assets/tray_icon.png"

const ID_SETTINGS := 1
const ID_ROAM := 2
const ID_EARCONS := 3
const ID_AMBIENT := 4
const ID_RESET_POS := 5
const ID_RELOAD_MCP := 6
const ID_OPEN_WEB := 7
const ID_QUIT := 99

var _indicator: StatusIndicator
var _menu: PopupMenu


func _ready() -> void:
	_menu = PopupMenu.new()
	_menu.name = "TrayMenu"
	add_child(_menu)

	_menu.add_item("⚙️ 设置与 API Key…", ID_SETTINGS)
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
		_indicator.menu = _indicator.get_path_to(_menu)


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


func popup_at_cursor() -> void:
	if _menu == null:
		return
	_menu.position = DisplayServer.mouse_get_position()
	_menu.popup()


func sync_toggles(roam_on: bool, earcons_on: bool, ambient_on: bool) -> void:
	if _menu == null:
		return
	_menu.set_item_checked(_menu.get_item_index(ID_ROAM), roam_on)
	_menu.set_item_checked(_menu.get_item_index(ID_EARCONS), earcons_on)
	_menu.set_item_checked(_menu.get_item_index(ID_AMBIENT), ambient_on)


func _on_menu_id_pressed(id: int) -> void:
	match id:
		ID_SETTINGS:
			open_settings_requested.emit()
		ID_ROAM:
			var idx := _menu.get_item_index(ID_ROAM)
			var next_val := not _menu.is_item_checked(idx)
			_menu.set_item_checked(idx, next_val)
			toggle_roam_requested.emit(next_val)
		ID_EARCONS:
			var idx := _menu.get_item_index(ID_EARCONS)
			var next_val := not _menu.is_item_checked(idx)
			_menu.set_item_checked(idx, next_val)
			toggle_earcons_requested.emit(next_val)
		ID_AMBIENT:
			var idx := _menu.get_item_index(ID_AMBIENT)
			var next_val := not _menu.is_item_checked(idx)
			_menu.set_item_checked(idx, next_val)
			toggle_ambient_requested.emit(next_val)
		ID_RESET_POS:
			reset_position_requested.emit()
		ID_RELOAD_MCP:
			reload_mcp_requested.emit()
		ID_OPEN_WEB:
			open_web_requested.emit()
		ID_QUIT:
			quit_requested.emit()
