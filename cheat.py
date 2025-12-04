
import pymem
import pymem.process
import time
import os
import threading
import tkinter as tk
import struct
import re
import math
import win32gui

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
known_targets = {}
targets_on_screen = []
is_running = True

# --- ОФФСЕТЫ ---
PROCESS_NAME = "FPVKamikazeDrone-Win64-Shipping.exe"
GAME_WINDOW_CLASS = "UnrealWindow"
GWORLD = 0x9DC11A0
PERSISTENT_LEVEL = 0x0030
ACTORS_ARRAY = 0xA0
ROOT_COMPONENT = 0x01B8
RELATIVE_LOCATION = 0x140 #0x0128 старый
OWNING_GAME_INSTANCE = 0x0228 #0x1D8 старый
LOCAL_PLAYERS = 0x38 #C0 возможно новый
PLAYER_CONTROLLER = 0x30
ACKNOWLEDGED_PAWN = 0x350
PLAYER_CAMERA_MANAGER_OFFSET = 0x0360
CAMERA_CACHE_OFFSET = 0x1530 #0x1410 старый
POV_OFFSET = 0x0010
PLAYER_STATE_IN_PAWN_OFFSET = 0x02C8 
IS_BOT_FLAG_OFFSET = 0x02B2
PLAYER_NAME_OFFSET = 0x0340


def read_fstring(pm, address):
    try:
        ptr = pm.read_longlong(address)
        if not ptr:
            return ""
        length = pm.read_int(address + 8)
        if not (0 < length < 128):
            return ""
        raw = pm.read_bytes(ptr, length * 2)
        decoded = raw.decode('utf-16', errors='ignore').strip('\x00')
        return ''.join(c for c in decoded if 32 <= ord(c) <= 126)
    except:
        return ""

# --- Функция WorldToScreen (без изменений) ---
def world_to_screen(world_location, cam_loc, cam_rot, fov, screen_width, screen_height):
    try:
        v_delta = (world_location[0] - cam_loc[0], world_location[1] - cam_loc[1], world_location[2] - cam_loc[2])
        yaw, pitch, roll = math.radians(cam_rot[1]), math.radians(cam_rot[0]), math.radians(cam_rot[2])
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        cr, sr = math.cos(roll), math.sin(roll)
        matrix = ((cp*cy,cp*sy,sp),(sr*sp*cy-cr*sy,sr*sp*sy+cr*cy,-sr*cp),(-(cr*sp*cy)-sr*sy,-(cr*sp*sy)+sr*cy,cr*cp))
        v_transformed = (v_delta[1]*matrix[0][1]+v_delta[0]*matrix[0][0]+v_delta[2]*matrix[0][2], v_delta[1]*matrix[1][1]+v_delta[0]*matrix[1][0]+v_delta[2]*matrix[1][2], v_delta[1]*matrix[2][1]+v_delta[0]*matrix[2][0]+v_delta[2]*matrix[2][2])
        if v_transformed[0] < 0.1: return None
        screen_center_x, screen_center_y = screen_width/2, screen_height/2
        screen_x = screen_center_x + v_transformed[1] * (screen_center_x/math.tan(math.radians(fov)/2))/v_transformed[0]
        screen_y = screen_center_y - v_transformed[2] * (screen_center_x/math.tan(math.radians(fov)/2))/v_transformed[0]
        return int(screen_x), int(screen_y)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None

# --- ИЗМЕНЕННЫЙ ПОТОК-СКАНЕР С ЖЕСТКИМ ФИЛЬТРОМ ---
def player_scanner_thread():
    global known_targets, is_running
    try:
        pm = pymem.Pymem(PROCESS_NAME)
        base_address = pymem.process.module_from_name(pm.process_handle, PROCESS_NAME).lpBaseOfDll
    except pymem.exception.ProcessNotFound: is_running = False; return

    while is_running:
        player_state_to_name = {} # {ps_ptr: "nickname"}
        pawn_to_display = {}      # {pawn_ptr: "display_name"}
        
        try:
            world_ptr = pm.read_longlong(base_address + GWORLD)
            level_ptr = pm.read_longlong(world_ptr + PERSISTENT_LEVEL)
            actors_array_ptr = pm.read_longlong(level_ptr + ACTORS_ARRAY)
            actor_count = pm.read_int(level_ptr + ACTORS_ARRAY + 0x8)

            # --- Шаг 1: Собираем "паспортный стол" всех игроков ---
            for i in range(actor_count):
                try:
                    actor_ptr = pm.read_longlong(actors_array_ptr + i * 0x8)
                    if not actor_ptr: continue
                    
                    ps_ptr = pm.read_longlong(actor_ptr + PLAYER_STATE_IN_PAWN_OFFSET)
                    if ps_ptr and ps_ptr not in player_state_to_name:
                        player_name = read_fstring(pm, ps_ptr + PLAYER_NAME_OFFSET)
                        if player_name:
                            flags_byte = pm.read_uchar(ps_ptr + IS_BOT_FLAG_OFFSET)
                            is_bot = (flags_byte & (1 << 3)) != 0
                            display_name = "Bot" if is_bot else player_name
                            player_state_to_name[ps_ptr] = display_name
                except pymem.exception.MemoryReadError:
                    continue

            # --- Шаг 2: Ищем "тела" (Pawn), которые принадлежат этим игрокам ---
            for i in range(actor_count):
                try:
                    actor_ptr = pm.read_longlong(actors_array_ptr + i * 0x8)
                    if not actor_ptr: continue

                    ps_ptr = pm.read_longlong(actor_ptr + PLAYER_STATE_IN_PAWN_OFFSET)
                    if ps_ptr in player_state_to_name:
                        # Нашли Pawn, который принадлежит известному нам игроку
                        pawn_to_display[actor_ptr] = player_state_to_name[ps_ptr]
                except pymem.exception.MemoryReadError:
                    continue
            
            known_targets = pawn_to_display

        except (pymem.exception.MemoryReadError, TypeError):
            pass
        time.sleep(1)
    print("Поток сканера завершен.")


# --- ОСНОВНОЙ ПОТОК (ОТРИСОВЩИК) (без изменений) ---
def cheat_thread():
    global targets_on_screen, is_running, known_targets
    try:
        pm = pymem.Pymem(PROCESS_NAME)
        base_address = pymem.process.module_from_name(pm.process_handle, PROCESS_NAME).lpBaseOfDll
    except pymem.exception.ProcessNotFound: is_running = False; return

    game_hwnd = win32gui.FindWindow(GAME_WINDOW_CLASS, None)
    if not game_hwnd: is_running = False; return
    rect = win32gui.GetWindowRect(game_hwnd)
    screen_width, screen_height = rect[2] - rect[0], rect[3] - rect[1]

    while is_running:
        try:
            world_ptr = pm.read_longlong(base_address + GWORLD)
            game_instance_ptr = pm.read_longlong(world_ptr + OWNING_GAME_INSTANCE)
            local_players_array_ptr = pm.read_longlong(game_instance_ptr + LOCAL_PLAYERS)
            local_player_ptr = pm.read_longlong(local_players_array_ptr)
            player_controller_ptr = pm.read_longlong(local_player_ptr + PLAYER_CONTROLLER)
            if not player_controller_ptr: continue
            
            camera_manager_ptr = pm.read_longlong(player_controller_ptr + PLAYER_CAMERA_MANAGER_OFFSET)
            if not camera_manager_ptr: continue
            
            pov_addr = camera_manager_ptr + CAMERA_CACHE_OFFSET + POV_OFFSET
            cam_loc = struct.unpack('ddd', pm.read_bytes(pov_addr + 0x0, 24))
            cam_rot = struct.unpack('ddd', pm.read_bytes(pov_addr + 0x18, 24))
            cam_fov = pm.read_float(pov_addr + 0x30)
            
            new_actors_list = []
            player_pawn_ptr = pm.read_longlong(player_controller_ptr + ACKNOWLEDGED_PAWN)
            
            current_targets_to_process = dict(known_targets)
            
            for actor_ptr, name in current_targets_to_process.items():
                if actor_ptr == player_pawn_ptr: continue

                try:
                    root_comp_ptr = pm.read_longlong(actor_ptr + ROOT_COMPONENT)
                    if not root_comp_ptr:
                        known_targets.pop(actor_ptr, None)
                        continue

                    actor_loc = struct.unpack('ddd', pm.read_bytes(root_comp_ptr + RELATIVE_LOCATION, 24))
                    screen_pos = world_to_screen(actor_loc, cam_loc, (cam_rot[0], cam_rot[1], cam_rot[2]), cam_fov, screen_width, screen_height)
                    
                    if screen_pos:
                        new_actors_list.append((*screen_pos, name))
                except (pymem.exception.MemoryReadError, struct.error):
                    known_targets.pop(actor_ptr, None)
                    continue
            targets_on_screen = new_actors_list
        except (pymem.exception.MemoryReadError, TypeError):
            pass
        time.sleep(0.001)
    print("Основной поток чита завершен.")

# --- GUI ДЛЯ ОТРИСОВКИ (без изменений) ---
def create_gui():
    global is_running
    root = tk.Tk()
    try:
        game_hwnd = win32gui.FindWindow(GAME_WINDOW_CLASS, None)
        rect = win32gui.GetWindowRect(game_hwnd)
        root.geometry(f"{rect[2]-rect[0]}x{rect[3]-rect[1]}+{rect[0]}+{rect[1]}")
    except:
        root.geometry("800x600")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", "black")
    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    def update_canvas():
        canvas.delete("all")
        current_actors = list(targets_on_screen)
        
        for x, y, name in current_actors:
            color = "yellow" if name == "Bot" else "cyan"
            canvas.create_oval(x-3, y-3, x+3, y+3, fill=color, outline=color)
            canvas.create_text(x, y - 10, text=name, fill=color, font=("Arial", 9, "bold"), anchor="s")

        info_text = f"Статус: Активен\nОтслеживаемых целей: {len(known_targets)}"
        canvas.create_text(10, 10, text=info_text, fill="lime", font=("Arial", 10, "bold"), anchor="nw")
        
        if not is_running:
            root.destroy()
        else:
            root.after(16, update_canvas)
    
    def on_closing():
        global is_running
        is_running = False
    
    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.after(16, update_canvas)
    root.mainloop()

# --- ЗАПУСК ПРОГРАММЫ (без изменений) ---
if __name__ == "__main__":
    print("Запуск потоков...")
    scanner = threading.Thread(target=player_scanner_thread, daemon=True)
    cheat = threading.Thread(target=cheat_thread, daemon=True)
    
    scanner.start()
    cheat.start()
    
    time.sleep(1)
    if is_running:
        print("Запуск GUI...")
        create_gui()
    
    print("Программа завершена.")
