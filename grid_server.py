import os
import time
import threading
import logging
import pyautogui
import pygetwindow as gw
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ==============================================================================
# CONFIGURATION
# ==============================================================================
SECOND_WINDOW_TITLE = os.getenv("SECOND_WINDOW_TITLE", "secondWindowTitle")
ACTION_DELAY = 0.5  # Seconds between UI actions

# Grid Specs (Relative to secondWindow)
GRID_START = (0, 15)
GRID_SIZE = (1062, 954)
PIN_BUTTON = (996, 54)
BACK_BUTTON = (63, 54)

# ==============================================================================
# UTILS & LOGGING
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Grid Interaction System")
click_lock = threading.Lock()

def get_target_window():
    """Find and activate the target window."""
    windows = gw.getWindowsWithTitle(SECOND_WINDOW_TITLE)
    if not windows:
        return None
    
    win = windows[0]
    if win.isMinimized:
        win.restore()
    win.activate()
    time.sleep(ACTION_DELAY)
    return win

def calculate_grid_center(grid_id: int):
    """
    Map 1-25 to (row, col) and calculate the center coordinate.
    Numbering: 1-5 (row 0), 6-10 (row 1), etc.
    """
    if not (1 <= grid_id <= 25):
        raise ValueError("Grid ID must be between 1 and 25")
    
    # 0-indexed adjustment
    idx = grid_id - 1
    row = idx // 5
    col = idx % 5
    
    cell_w = GRID_SIZE[0] / 5
    cell_h = GRID_SIZE[1] / 5
    
    rel_x = GRID_START[0] + (col + 0.5) * cell_w
    rel_y = GRID_START[1] + (row + 0.5) * cell_h
    
    return int(rel_x), int(rel_y)

# ==============================================================================
# ROUTES
# ==============================================================================

@app.post("/grid")
@app.get("/grid")
def click_grid(id: int):
    """Click a grid cell and then the Pin button."""
    if not (1 <= id <= 25):
        raise HTTPException(status_code=400, detail="Invalid Grid ID. Must be 1-25.")

    if not click_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="System busy. Another action is in progress.")
    
    try:
        win = get_target_window()
        if not win:
            raise HTTPException(status_code=404, detail=f"Window '{SECOND_WINDOW_TITLE}' not found.")
        
        # 1. Click Grid Center
        rel_x, rel_y = calculate_grid_center(id)
        abs_x, abs_y = win.left + rel_x, win.top + rel_y
        
        logger.info(f"Clicking grid {id} at ({abs_x}, {abs_y})")
        pyautogui.click(abs_x, abs_y)
        time.sleep(ACTION_DELAY)
        
        # 2. Click Pin Button
        pin_x, pin_y = win.left + PIN_BUTTON[0], win.top + PIN_BUTTON[1]
        logger.info(f"Clicking Pin button at ({pin_x}, {pin_y})")
        pyautogui.click(pin_x, pin_y)
        
        return {
            "status": "success",
            "action": f"grid_{id}_and_pin",
            "target": {"x": abs_x, "y": abs_y},
            "pin": {"x": pin_x, "y": pin_y}
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during grid click: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        click_lock.release()

@app.post("/back")
@app.get("/back")
def click_back():
    """Click the Back button."""
    if not click_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="System busy. Another action is in progress.")
    
    try:
        win = get_target_window()
        if not win:
            raise HTTPException(status_code=404, detail=f"Window '{SECOND_WINDOW_TITLE}' not found.")
        
        back_x, back_y = win.left + BACK_BUTTON[0], win.top + BACK_BUTTON[1]
        logger.info(f"Clicking Back button at ({back_x}, {back_y})")
        pyautogui.click(back_x, back_y)
        
        return {
            "status": "success",
            "action": "back",
            "target": {"x": back_x, "y": back_y}
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during back click: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        click_lock.release()

def run_server(host="0.0.0.0", port=8000):
    """Start the FastAPI server."""
    import uvicorn
    logger.info(f"Starting Grid Interaction Server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    run_server()
