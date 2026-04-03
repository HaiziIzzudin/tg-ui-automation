import os
import time
import subprocess
import logging
import pyautogui
import pygetwindow as gw
import psutil
import ctypes
import threading
from dotenv import load_dotenv
import grid_server

# Load environment variables from .env file
load_dotenv()

# ==============================================================================
# CONFIGURATION
# ==============================================================================
TELEGRAM_EXE = "Telegram.exe"
TELEGRAM_PATH = r"C:\Users\haizi\AppData\Roaming\Telegram Desktop\Telegram.exe"
TELEGRAM_TITLE = "Telegram"

# Title of the secondary window (fetched from .env or fallback)
SECOND_WINDOW_TITLE = os.getenv("SECOND_WINDOW_TITLE", "secondWindowTitle") 

MONITOR_INTERVAL = 3.0      # Seconds to wait between process checks
LAUNCH_TIMEOUT = 7.0        # Seconds to wait for Telegram to launch
ACTION_DELAY = 0.5          # Seconds to wait between window actions (resize/click)

TELEGRAM_SIZE = (800, 600)
SECOND_WINDOW_SIZE = (1280, 1032)
SECOND_WINDOW_POS = (0, 0)

CLICK_COORDS = [
    (44, 240),
    (207, 119),
    (755, 113)
]

# ==============================================================================
# LOGGING SETUP
# ==============================================================================
# Set up logging to both console and a file in the same directory as the script.
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_monitor.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# CORE FUNCTIONS
# ==============================================================================

def is_telegram_running() -> bool:
    """Check if the Telegram process is currently running."""
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == TELEGRAM_EXE.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return False

def kill_telegram():
    """Terminate the Telegram process."""
    logger.info(f"Attempting to terminate {TELEGRAM_EXE}...")
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == TELEGRAM_EXE.lower():
                proc.terminate()
                proc.wait(timeout=5)
                logger.info(f"{TELEGRAM_EXE} terminated.")
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except psutil.TimeoutExpired:
            logger.warning(f"{TELEGRAM_EXE} did not terminate in time, killing...")
            try:
                proc.kill()
            except:
                pass
    return False

def launch_telegram() -> bool:
    """Launch Telegram and wait for the window to appear."""
    logger.info(f"Launching Telegram from: {TELEGRAM_PATH}")
    try:
        subprocess.Popen(TELEGRAM_PATH)
    except Exception as e:
        logger.error(f"Failed to launch Telegram: {e}")
        return False

    # Wait for the process and window to register
    start_time = time.time()
    while time.time() - start_time < LAUNCH_TIMEOUT:
        windows = gw.getWindowsWithTitle(TELEGRAM_TITLE)
        # Filter for exact match or primary window if possible
        target_win = None
        for w in windows:
            if w.title == TELEGRAM_TITLE:
                target_win = w
                break
        
        if target_win:
            logger.info("Telegram window found.")
            time.sleep(ACTION_DELAY) # Give UI a moment to fully render
            return True
        time.sleep(0.5)
        
    logger.error(f"Timeout: Telegram window did not appear after {LAUNCH_TIMEOUT} seconds.")
    return False

def perform_telegram_actions():
    """Resize the Telegram window and perform the click sequence."""
    windows = gw.getWindowsWithTitle(TELEGRAM_TITLE)
    target_win = next((w for w in windows if w.title == TELEGRAM_TITLE), None)
    
    if not target_win:
        logger.error("Could not find Telegram window for actions.")
        return False
        
    try:
        # 1. Resize Telegram
        logger.info(f"Resizing Telegram window to {TELEGRAM_SIZE}...")
        if target_win.isMinimized:
            target_win.restore()
        
        target_win.resizeTo(TELEGRAM_SIZE[0], TELEGRAM_SIZE[1])
        target_win.activate() # Bring to front
        time.sleep(ACTION_DELAY)
        
        # 2. Perform Clicks
        win_x, win_y = target_win.topleft
        logger.info(f"Telegram Window position: ({win_x}, {win_y})")
        
        for idx, (offset_x, offset_y) in enumerate(CLICK_COORDS):
            target_x = win_x + offset_x
            target_y = win_y + offset_y
            logger.info(f"Action {idx+1}/{len(CLICK_COORDS)}: Clicking at relative ({offset_x}, {offset_y}) -> absolute ({target_x}, {target_y})")
            
            # Move slowly to coordinate and click (aids debugging visually initially)
            pyautogui.moveTo(target_x, target_y, duration=0.2)
            pyautogui.click()
            time.sleep(ACTION_DELAY)
            
        return True
    
    except Exception as e:
        logger.error(f"Error during Telegram actions: {e}")
        return False

def setup_second_window() -> bool:
    """Find, resize, and move the secondary target window."""
    logger.info(f"Searching for secondary window: '{SECOND_WINDOW_TITLE}'...")
    windows = gw.getWindowsWithTitle(SECOND_WINDOW_TITLE)
    
    if not windows:
        logger.error(f"Secondary window ('{SECOND_WINDOW_TITLE}') not found. Ensure title is correct.")
        return False
        
    try:
        target_win = windows[0] # Assume the first matching window
        
        if target_win.isMinimized:
            target_win.restore()
            
        logger.info(f"Moving '{SECOND_WINDOW_TITLE}' to {SECOND_WINDOW_POS}...")
        target_win.moveTo(SECOND_WINDOW_POS[0], SECOND_WINDOW_POS[1])
        time.sleep(ACTION_DELAY)

        logger.info(f"Resizing '{SECOND_WINDOW_TITLE}' to {SECOND_WINDOW_SIZE}...")
        target_win.resizeTo(SECOND_WINDOW_SIZE[0], SECOND_WINDOW_SIZE[1])
        time.sleep(ACTION_DELAY)
        
        return True
    except Exception as e:
        logger.error(f"Error adjusting secondary window: {e}")
        return False

def ensure_correct_resolution():
    """Check if current resolution is 1920x1080, if not, fix it."""
    user32 = ctypes.windll.user32
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)

    if width != 1920 or height != 1080:
        logger.warning(f"Resolution mismatch detected: {width}x{height}. Expected 1920x1080. Fixing...")
        try:
            exe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SetResolution.exe")
            subprocess.run(['powershell', f'& "{exe_path}" 1080 -noprompt'], check=True)
            logger.info("Resolution change command sent.")
            # Wait a bit for the resolution change to take effect
            time.sleep(2)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to change resolution: {e}")
    else:
        logger.debug("Screen resolution is correct (1920x1080).")

# ==============================================================================
# MAIN LOOP
# ==============================================================================

def main():
    logger.info("Starting Telegram Monitor Service...")
    
    # Start Grid Interaction Server in a background thread
    logger.info("Launching Grid Interaction Server (FastAPI) thread...")
    api_thread = threading.Thread(target=grid_server.run_server, daemon=True)
    api_thread.start()
    
    while True:
        try:
            # Always ensure resolution is correct first
            ensure_correct_resolution()
            
            telegram_running = is_telegram_running()
            
            if not telegram_running:
                logger.warning(f"Process {TELEGRAM_EXE} not found. Initiating setup sequence...")
                
                # 1. Launch Telegram
                if not launch_telegram():
                    logger.error("Failed to launch Telegram. Re-checking in next cycle.")
                    time.sleep(MONITOR_INTERVAL)
                    continue
                
                # 2. Automate Telegram window (resize, clicks)
                if not perform_telegram_actions():
                    logger.error("Failed to complete Telegram actions. Re-checking in next cycle.")
                    time.sleep(MONITOR_INTERVAL)
                    continue
                    
                # 3. Setup Second Window
                if not setup_second_window():
                    logger.warning("Secondary window not found after setup. Killing Telegram and waiting 60 seconds before retry...")
                    kill_telegram()
                    time.sleep(30)
                    continue
                
                logger.info("Setup sequence completed successfully. Entering monitoring mode.")
            
            else:
                # Telegram IS running. Now we must verify the second window is also present.
                # If it's missing, we assume a failure state and perform recovery.
                windows = gw.getWindowsWithTitle(SECOND_WINDOW_TITLE)
                if not windows:
                    logger.warning(f"Telegram is running but secondary window '{SECOND_WINDOW_TITLE}' is missing. Initiating recovery...")
                    kill_telegram()
                    time.sleep(60)
                    continue
                
            # Sleep until the next check
            time.sleep(MONITOR_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("Monitor Service stopped by user.")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            time.sleep(MONITOR_INTERVAL)

if __name__ == "__main__":
    main()
