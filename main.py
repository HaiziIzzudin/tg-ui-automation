import os
import time
import subprocess
import logging
import pyautogui
import pygetwindow as gw
import psutil
import ctypes
from dotenv import load_dotenv
from config import Config

# Load environment variables from .env file
load_dotenv()

# ==============================================================================
# CONFIGURATION
# ==============================================================================
TELEGRAM_EXE = "Telegram.exe"
TELEGRAM_PATH = r"C:\Users\haizi\AppData\Roaming\Telegram Desktop\Telegram.exe"
TELEGRAM_TITLE = "Telegram"

MONITOR_INTERVAL = 3.0      # Seconds to wait between process checks
LAUNCH_TIMEOUT = 7.0        # Seconds to wait for Telegram to launch
ACTION_DELAY = 0.5          # Seconds to wait between window actions (resize/click)


TELEGRAM_SIZE = (800, 600)

# Initialize config from environment variables
config = Config()

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
        return perform_click_sequence()
    
    except Exception as e:
        logger.error(f"Error during Telegram actions: {e}")
        return False

def perform_click_sequence() -> bool:
    """Perform the click sequence on the Telegram window without resizing."""
    windows = gw.getWindowsWithTitle(TELEGRAM_TITLE)
    target_win = next((w for w in windows if w.title == TELEGRAM_TITLE), None)
    
    if not target_win:
        logger.error("Could not find Telegram window for click sequence.")
        return False
    
    if not config.click_points:
        logger.warning("No click points configured. Skipping click sequence.")
        return True
    
    try:
        if target_win.isMinimized:
            target_win.restore()
        target_win.activate()
        time.sleep(ACTION_DELAY)
        
        win_x, win_y = target_win.topleft
        logger.info(f"Telegram Window position: ({win_x}, {win_y})")
        
        for idx, (offset_x, offset_y) in enumerate(config.click_points):
            target_x = win_x + offset_x
            target_y = win_y + offset_y
            logger.info(f"Action {idx+1}/{len(config.click_points)}: Clicking at relative ({offset_x}, {offset_y}) -> absolute ({target_x}, {target_y})")
            
            pyautogui.moveTo(target_x, target_y, duration=0.2)
            pyautogui.click()
            time.sleep(ACTION_DELAY)
        
        return True
    except Exception as e:
        logger.error(f"Error during click sequence: {e}")
        return False

def setup_second_window() -> bool:
    """Find, resize, and move the secondary target window."""
    logger.info(f"Searching for secondary window: '{config.second_window_title}'...")
    windows = gw.getWindowsWithTitle(config.second_window_title)
    
    if not windows:
        logger.error(f"Secondary window ('{config.second_window_title}') not found. Ensure title is correct.")
        return False
        
    try:
        target_win = windows[0] # Assume the first matching window
        
        if target_win.isMinimized:
            target_win.restore()
            
        # If config.second_window_size is None, maximize the window
        if config.second_window_size is None:
            logger.info(f"Maximizing '{config.second_window_title}'...")
            target_win.maximize()
        else:
            screen_w, screen_h = config.get_screen_bounds()
            req_w, req_h = config.second_window_size
            clamped_w = min(req_w, screen_w)
            clamped_h = min(req_h, screen_h)
            if clamped_w != req_w or clamped_h != req_h:
                logger.warning(
                    f"Requested size {req_w}x{req_h} exceeds screen bounds "
                    f"{screen_w}x{screen_h}; clamping to {clamped_w}x{clamped_h}."
                )

            logger.info(f"Moving '{config.second_window_title}' to {config.second_window_pos}...")
            target_win.moveTo(config.second_window_pos[0], config.second_window_pos[1])
            time.sleep(ACTION_DELAY)

            logger.info(f"Resizing '{config.second_window_title}' to {clamped_w}x{clamped_h}...")
            target_win.resizeTo(clamped_w, clamped_h)
        time.sleep(ACTION_DELAY)
        
        return True
    except Exception as e:
        logger.error(f"Error adjusting secondary window: {e}")
        return False

def ensure_correct_resolution():
    """Check if current resolution matches configured values, if not, fix it."""
    user32 = ctypes.windll.user32
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)

    expected_width, expected_height = config.get_expected_resolution()
    rotation = config.screen_rotation
    if width != expected_width or height != expected_height:
        logger.warning(
            f"Resolution mismatch detected: {width}x{height} (rotation {rotation}). "
            f"Expected {expected_width}x{expected_height}. Fixing..."
        )
        try:
            exe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SetResolution.exe")
            subprocess.run(
                ['powershell', f'& "{exe_path}" SET -w {expected_width} -h {expected_height} -o {rotation} -noprompt'],
                check=True,
            )
            logger.info("Resolution change command sent.")
            # Wait a bit for the resolution change to take effect
            time.sleep(2)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to change resolution: {e}")
    else:
        logger.debug(f"Screen resolution is correct ({expected_width}x{expected_height}, rotation {rotation}).")

# ==============================================================================
# MAIN LOOP
# ==============================================================================

def main():
    logger.info("Starting Telegram Monitor Service...")

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
                    logger.warning("Secondary window not found after setup. Killing Telegram and waiting %d seconds before retry...", config.kill_cooldown_seconds)
                    kill_telegram()
                    time.sleep(config.kill_cooldown_seconds)
                    continue
                
                logger.info("Setup sequence completed successfully. Entering monitoring mode.")
            
            else:
                # Telegram IS running. Now we must verify the second window is also present.
                # If it's missing, re-run the Click Sequence up to the retry limit.
                windows = gw.getWindowsWithTitle(config.second_window_title)
                if not windows:
                    logger.warning(f"Telegram is running but secondary window '{config.second_window_title}' is missing.")
                    retry_count = 0
                    while retry_count < config.second_window_retry_limit:
                        logger.info(f"Click Retry attempt {retry_count + 1}/{config.second_window_retry_limit}")
                        perform_click_sequence()
                        time.sleep(config.click_retry_interval)
                        windows = gw.getWindowsWithTitle(config.second_window_title)
                        if windows:
                            logger.info(f"Target window found after {retry_count + 1} attempt(s).")
                            break
                        retry_count += 1

                    if not windows:
                        logger.warning("Click Retry limit exhausted. Initiating Recovery...")
                        kill_telegram()
                        time.sleep(config.kill_cooldown_seconds)
                        continue
                    else:
                        # Window found after retry, apply size/position settings
                        if not setup_second_window():
                            logger.warning("Failed to setup second window after click retry.")
                
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
