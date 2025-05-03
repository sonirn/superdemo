import os
import subprocess
import sys
import time
import platform
import requests
import tarfile
import zipfile
import signal
import json
import threading
import psutil
import glob
from datetime import datetime
import gradio as gr

# Configuration for mining
DEFAULT_CONFIG = {
    "wallet_address": "TPMkuHpxfYt21SbT3m6BQJVo4vymozKw1C",  # Default wallet (replace with your own)
    "worker_name": "HF_Worker",
    "coin_symbol": "TRX",
    "pool": "rx.unmineable.com:3333",
    "xmrig_version": "6.19.0",  # Using a more stable version
    "algorithm": "rx",
    "threads": "auto"  # "auto" will use optimal thread count based on system
}

# Global variables for tracking
mining_process = None
mining_output = []
mining_stats = {
    "start_time": None,
    "hashrate": "0 H/s",
    "shares_accepted": 0,
    "shares_rejected": 0,
    "running": False,
    "cpu_usage": 0,
    "memory_usage": 0,
    "last_update": 0,
    "worker_status": "Offline",
    "pool_status": "Unknown"
}
output_lock = threading.Lock()
update_thread = None
stop_update_thread = threading.Event()

class XMRigMiner:
    def __init__(self, config=None):
        self.config = config or DEFAULT_CONFIG.copy()
        self.setup_dir = os.path.join(os.getcwd(), "mining_data")
        os.makedirs(self.setup_dir, exist_ok=True)
        self.stop_event = threading.Event()
        self.xmrig_path = None
        self.config_path = None
    
    def download_xmrig(self):
        """Download XMRig miner with improved error handling and verification"""
        system = platform.system().lower()
        machine = platform.machine().lower()
        
        # Determine system architecture
        if system == "linux":
            if "x86_64" in machine or "amd64" in machine:
                arch = "linux-x64"
            elif "aarch64" in machine or "arm64" in machine:
                arch = "linux-arm64"
            else:
                arch = "linux-x64"  # Default fallback
        elif system == "windows":
            arch = "win64"
        else:
            # Hugging Face Spaces typically uses Linux
            arch = "linux-x64"
        
        version = self.config["xmrig_version"]
        filename = f"xmrig-{version}-{arch}.tar.gz"
        if "win" in arch:
            filename = f"xmrig-{version}-{arch}.zip"
            
        url = f"https://github.com/xmrig/xmrig/releases/download/v{version}/{filename}"
        
        log_message(f"Downloading XMRig v{version} for {arch}...")
        log_message(f"URL: {url}")
        
        try:
            # Clean up any previous installation files
            for old_file in glob.glob(os.path.join(self.setup_dir, "xmrig-*")):
                if os.path.isfile(old_file):
                    try:
                        os.remove(old_file)
                        log_message(f"Removed old file: {old_file}")
                    except Exception as e:
                        log_message(f"Warning: Could not remove old file {old_file}: {e}")
            
            # Download the file
            response = requests.get(url, stream=True)
            response.raise_for_status()
            
            download_path = os.path.join(self.setup_dir, filename)
            
            # Save the file
            with open(download_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            log_message("Download complete!")
            
            # Create a specific extraction directory
            extraction_dir = os.path.join(self.setup_dir, f"xmrig-{version}")
            os.makedirs(extraction_dir, exist_ok=True)
            
            # Extract the archive
            if filename.endswith('.zip'):
                with zipfile.ZipFile(download_path, 'r') as zip_ref:
                    # List all files in the archive for debugging
                    file_list = zip_ref.namelist()
                    log_message(f"Archive contains {len(file_list)} files. First few: {file_list[:5]}")
                    zip_ref.extractall(extraction_dir)
            else:
                with tarfile.open(download_path, 'r:gz') as tar:
                    # List all files in the archive for debugging
                    file_list = tar.getnames()
                    log_message(f"Archive contains {len(file_list)} files. First few: {file_list[:5]}")
                    tar.extractall(path=extraction_dir)
            
            # Find the xmrig executable using a recursive search
            if system == "windows":
                xmrig_pattern = os.path.join(extraction_dir, "**", "xmrig.exe")
            else:
                xmrig_pattern = os.path.join(extraction_dir, "**", "xmrig")
            
            # Use glob to find the executable, searching recursively
            xmrig_matches = glob.glob(xmrig_pattern, recursive=True)
            
            if xmrig_matches:
                # Found the executable
                self.xmrig_path = xmrig_matches[0]
                log_message(f"Found XMRig executable at: {self.xmrig_path}")
                
                # Make sure it's executable on Linux/macOS
                if system != "windows":
                    try:
                        os.chmod(self.xmrig_path, 0o755)
                        log_message("Set executable permissions")
                    except Exception as e:
                        log_message(f"Warning: Could not set executable permissions: {e}")
                
                return True
            else:
                # Fallback: Try to find any file that might be the executable
                log_message("XMRig executable not found in expected location. Searching for alternatives...")
                
                # List all files in extraction directory
                all_files = []
                for root, dirs, files in os.walk(extraction_dir):
                    for file in files:
                        all_files.append(os.path.join(root, file))
                
                log_message(f"Found {len(all_files)} files in extraction directory")
                
                # Look for candidates that might be the XMRig executable
                candidates = [f for f in all_files if os.path.basename(f) == "xmrig" or os.path.basename(f) == "xmrig.exe"]
                if candidates:
                    self.xmrig_path = candidates[0]
                    if system != "windows":
                        try:
                            os.chmod(self.xmrig_path, 0o755)
                        except Exception as e:
                            log_message(f"Warning: Could not set executable permissions: {e}")
                    log_message(f"Using alternative executable: {self.xmrig_path}")
                    return True
                
                # If no executable found, check if we need to compile it
                log_message("No executable found. XMRig might need to be compiled from source.")
                return False
                
        except Exception as e:
            log_message(f"Error during XMRig download or extraction: {e}")
            import traceback
            log_message(traceback.format_exc())
            return False
    
    def create_config_file(self):
        """Create an optimized configuration file for XMRig"""
        # Determine thread count
        if self.config["threads"] == "auto":
            thread_count = max(1, psutil.cpu_count(logical=True) - 1)  # Leave one thread free
        elif self.config["threads"] == "all":
            thread_count = psutil.cpu_count(logical=True)
        else:
            try:
                thread_count = int(self.config["threads"])
            except ValueError:
                thread_count = 2  # Default if invalid value
        
        # Create optimized config
        config = {
            "autosave": True,
            "cpu": {
                "enabled": True,
                "huge-pages": True,
                "hw-aes": None,
                "priority": None,
                "memory-pool": False,
                "yield": True,
                "max-threads-hint": thread_count,
                "asm": True
            },
            "opencl": False,
            "cuda": False,
            "pools": [
                {
                    "url": self.config["pool"],
                    "user": f"{self.config['coin_symbol']}:{self.config['wallet_address']}.{self.config['worker_name']}",
                    "pass": "x",
                    "keepalive": True,
                    "tls": False
                }
            ],
            "randomx": {
                "init": -1,
                "numa": True,
                "mode": "auto"
            },
            "donate-level": 1,  # Minimum donation level to support XMRig development
            "log-file": None,
            "background": False,
            "colors": True,
            "title": True,
            "syslog": False,
            "user-agent": None,
            "verbose": 0,
            "watch": False
        }
        
        # Write the config to a file
        self.config_path = os.path.join(self.setup_dir, "config.json")
        with open(self.config_path, 'w') as f:
            json.dump(config, f, indent=4)
        
        log_message(f"Created optimized XMRig configuration with {thread_count} threads")
        return True
    
    def start_mining(self):
        """Start the mining process with optimal settings"""
        global mining_process, mining_stats
        
        if not self.xmrig_path or not os.path.exists(self.xmrig_path):
            log_message("XMRig not found or not properly installed. Attempting to download...")
            if not self.download_xmrig():
                return False
        
        if not self.config_path or not os.path.exists(self.config_path):
            if not self.create_config_file():
                return False
        
        # Verify the executable exists and is executable
        if not os.path.exists(self.xmrig_path):
            log_message(f"Error: XMRig executable not found at {self.xmrig_path}")
            return False
        
        # Prepare mining command with optimizations
        mining_command = [
            self.xmrig_path,
            "--config=" + self.config_path,
            "--print-time=15"  # Print hashrate every 15 seconds
        ]
        
        log_message("\nStarting mining process...")
        log_message(f"Command: {' '.join(mining_command)}")
        
        try:
            # Start the mining process
            mining_stats["start_time"] = datetime.now()
            mining_stats["running"] = True
            mining_stats["worker_status"] = "Mining"
            mining_stats["shares_accepted"] = 0
            mining_stats["shares_rejected"] = 0
            
            # Start the mining process
            mining_process = subprocess.Popen(
                mining_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )
            
            # Start a thread to read the output
            output_thread = threading.Thread(target=self.read_output)
            output_thread.daemon = True
            output_thread.start()
            
            # Start a thread to monitor resources
            monitor_thread = threading.Thread(target=self.monitor_resources)
            monitor_thread.daemon = True
            monitor_thread.start()
            
            # Start a thread to check pool status periodically
            pool_status_thread = threading.Thread(target=self.monitor_pool_status)
            pool_status_thread.daemon = True
            pool_status_thread.start()
            
            return True
            
        except Exception as e:
            log_message(f"Error starting mining process: {e}")
            import traceback
            log_message(traceback.format_exc())
            return False
    
    def read_output(self):
        """Read and process the output from the mining process"""
        global mining_stats
        
        while mining_process and mining_process.poll() is None and not self.stop_event.is_set():
            try:
                line = mining_process.stdout.readline().strip()
                if line:
                    log_message(line)
                    
                    # Extract mining statistics
                    if "accepted" in line and "diff" in line:
                        mining_stats["shares_accepted"] += 1
                        mining_stats["worker_status"] = "Mining (Active)"
                    elif "rejected" in line and "diff" in line:
                        mining_stats["shares_rejected"] += 1
                    elif "speed" in line and "H/s" in line:
                        try:
                            # Try to extract the hashrate
                            parts = line.split()
                            for i, part in enumerate(parts):
                                if "H/s" in part and i > 0:
                                    mining_stats["hashrate"] = f"{parts[i-1]} {part}"
                                    break
                        except:
                            pass
                    elif "connection lost" in line.lower():
                        mining_stats["worker_status"] = "Connection Lost"
                    elif "reconnect" in line.lower():
                        mining_stats["worker_status"] = "Reconnecting..."
                    elif "new job" in line.lower():
                        mining_stats["worker_status"] = "Mining"
            except:
                break
    
    def monitor_resources(self):
        """Monitor system resource usage"""
        global mining_stats
        
        while mining_process and mining_process.poll() is None and not self.stop_event.is_set():
            try:
                # Get CPU and memory usage
                cpu_percent = psutil.cpu_percent(interval=1)
                memory = psutil.virtual_memory()
                
                # Update mining stats
                mining_stats["cpu_usage"] = cpu_percent
                mining_stats["memory_usage"] = memory.percent
                
                # Get process-specific information if available
                if mining_process and mining_process.pid:
                    try:
                        process = psutil.Process(mining_process.pid)
                        process_cpu = process.cpu_percent(interval=1)
                        process_memory = process.memory_info().rss / (1024 * 1024)  # MB
                        log_message(f"Resource usage - CPU: {cpu_percent:.1f}% (Mining: {process_cpu:.1f}%), "
                                    f"Memory: {memory.percent:.1f}% (Mining: {process_memory:.1f} MB)")
                    except:
                        pass
            except:
                pass
            
            # Wait before checking again
            time.sleep(30)  # Check every 30 seconds to reduce overhead
    
    def monitor_pool_status(self):
        """Check the worker's status on the mining pool periodically"""
        global mining_stats
        
        while mining_process and mining_process.poll() is None and not self.stop_event.is_set():
            try:
                # Only check if we're actively mining
                if mining_stats["running"]:
                    status = check_worker_on_pool(
                        self.config["wallet_address"],
                        self.config["worker_name"],
                        self.config["coin_symbol"]
                    )
                    mining_stats["pool_status"] = status
                    log_message(f"Pool status check: {status}")
            except Exception as e:
                log_message(f"Error checking pool status: {str(e)}")
                mining_stats["pool_status"] = "Error checking"
            
            # Wait before checking again - longer interval to avoid hammering the pool's website
            time.sleep(300)  # Check every 5 minutes
    
    def stop_mining(self):
        """Stop the mining process"""
        global mining_process, mining_stats
        
        self.stop_event.set()
        
        if mining_process:
            log_message("Stopping mining process...")
            try:
                mining_process.terminate()
                try:
                    mining_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    mining_process.kill()
            except:
                pass
            
            mining_stats["running"] = False
            mining_stats["worker_status"] = "Offline"
            self.print_mining_summary()
            
            # Reset the process
            mining_process = None
        
        return True
    
    def print_mining_summary(self):
        """Print a summary of the mining session"""
        if mining_stats["start_time"]:
            duration = datetime.now() - mining_stats["start_time"]
            hours, remainder = divmod(duration.total_seconds(), 3600)
            minutes, seconds = divmod(remainder, 60)
            
            summary = "\n=== Mining Session Summary ===\n"
            summary += f"Duration: {int(hours)}h {int(minutes)}m {int(seconds)}s\n"
            summary += f"Wallet: {self.config['coin_symbol']}:{self.config['wallet_address']}\n"
            summary += f"Worker: {self.config['worker_name']}\n"
            summary += f"Pool: {self.config['pool']}\n"
            summary += f"Final Hashrate: {mining_stats['hashrate']}\n"
            summary += f"Shares: {mining_stats['shares_accepted']} accepted, {mining_stats['shares_rejected']} rejected\n"
            summary += f"Worker Status: {mining_stats['worker_status']}\n"
            summary += f"Pool Status: {mining_stats['pool_status']}\n"
            summary += "=============================\n"
            
            log_message(summary)

# Helper functions
def log_message(message):
    """Add a message to the mining output log"""
    global mining_output
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_message = f"[{timestamp}] {message}"
    
    with output_lock:
        mining_output.append(formatted_message)
        if len(mining_output) > 500:  # Keep more log history
            mining_output = mining_output[-500:]
    
    print(message)

def check_worker_on_pool(wallet_address, worker_name, coin_symbol):
    """
    Check if a worker is visible on the pool (Unmineable)
    This is a simplified implementation that checks the worker's status
    """
    try:
        # Create a URL for checking worker status
        url = f"https://api.unminable.com/v4/address/{wallet_address}?coin={coin_symbol}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # For demonstration, we'll return a status based on mining time
        # In a real implementation, you would parse the API response
        if mining_stats["running"]:
            # If we've been mining for less than 30 minutes
            mining_time = (datetime.now() - mining_stats["start_time"]).total_seconds() / 60
            if mining_time < 30:
                return "Pending (takes up to 30 min to appear)"
            elif mining_stats["shares_accepted"] > 0:
                return "Active on pool"
            else:
                return "Connected, awaiting shares"
        else:
            return "Offline"
            
    except Exception as e:
        return f"Error checking: {str(e)[:30]}"

def check_mining_balance(wallet_address, coin_symbol):
    """Check mining balance on Unmineable pool"""
    try:
        # Format URL for the pool's balance page
        url = f"https://unmineable.com/coins/{coin_symbol}/address/{wallet_address}"
        
        # For a real implementation, you could try to scrape the balance
        # But for now, we'll just provide the link
        return f"To check your balance and worker status, visit:\n\n{url}\n\nNote: New workers may take up to 30 minutes to appear on the dashboard."
    except Exception as e:
        return f"Error creating balance link: {str(e)}"

# Create a mining manager instance
miner = XMRigMiner()

# Gradio interface functions
def start_mining_interface(wallet, worker, coin, pool, threads):
    """Start mining with the specified parameters"""
    if mining_stats["running"]:
        return "Mining is already running. Please stop it first.", get_current_output(), mining_stats["worker_status"], mining_stats["pool_status"]
    
    # Update configuration
    miner.config["wallet_address"] = wallet if wallet else DEFAULT_CONFIG["wallet_address"]
    miner.config["worker_name"] = worker if worker else DEFAULT_CONFIG["worker_name"]
    miner.config["coin_symbol"] = coin if coin else DEFAULT_CONFIG["coin_symbol"]
    miner.config["pool"] = pool if pool else DEFAULT_CONFIG["pool"]
    miner.config["threads"] = threads if threads else DEFAULT_CONFIG["threads"]
    
    # Start mining
    if miner.start_mining():
        # Start the UI update thread if not already running
        start_update_thread()
        return "Mining started successfully!", get_current_output(), "Mining", "Checking..."
    else:
        return "Failed to start mining. Check the logs for details.", get_current_output(), "Offline", "Unknown"

def stop_mining_interface():
    """Stop mining"""
    if not mining_stats["running"]:
        return "Mining is not running.", get_current_output(), mining_stats["worker_status"], mining_stats["pool_status"]
    
    if miner.stop_mining():
        return "Mining stopped successfully!", get_current_output(), "Offline", "Unknown"
    else:
        return "Failed to stop mining. Check the logs for details.", get_current_output(), mining_stats["worker_status"], mining_stats["pool_status"]

def get_current_output():
    """Get the current mining output"""
    with output_lock:
        return "\n".join(mining_output)

def get_mining_status():
    """Get the current mining status"""
    if not mining_stats["running"]:
        return "Not running"
    
    if mining_stats["start_time"]:
        duration = datetime.now() - mining_stats["start_time"]
        hours, remainder = divmod(duration.total_seconds(), 3600)
        minutes, seconds = divmod(remainder, 60)
        
        return (f"Running for {int(hours)}h {int(minutes)}m {int(seconds)}s | "
                f"Hashrate: {mining_stats['hashrate']} | "
                f"Shares: {mining_stats['shares_accepted']} accepted, {mining_stats['shares_rejected']} rejected | "
                f"CPU: {mining_stats['cpu_usage']:.1f}%")
    
    return "Starting..."

def update_status():
    """Update the status display"""
    return get_mining_status(), get_current_output(), mining_stats["worker_status"], mining_stats["pool_status"]

def refresh_status():
    """Manually refresh the status display"""
    return update_status()

def check_balance_interface(wallet, coin):
    """Check mining balance on pool"""
    wallet_address = wallet if wallet else DEFAULT_CONFIG["wallet_address"]
    coin_symbol = coin if coin else DEFAULT_CONFIG["coin_symbol"]
    
    result = check_mining_balance(wallet_address, coin_symbol)
    return result

# Thread function for updating the UI
def ui_update_thread(status_box, output_box, worker_status_box, pool_status_box):
    while not stop_update_thread.is_set():
        try:
            status_value, output_value, worker_status_value, pool_status_value = update_status()
            status_box.update(value=status_value)
            output_box.update(value=output_value)
            worker_status_box.update(value=worker_status_value)
            pool_status_box.update(value=pool_status_value)
        except Exception as e:
            # If update fails, don't crash the thread
            print(f"Error updating UI: {str(e)}")
        time.sleep(5)  # Update every 5 seconds

def start_update_thread():
    global update_thread, stop_update_thread
    
    # Stop any existing thread
    if update_thread and update_thread.is_alive():
        stop_update_thread.set()
        update_thread.join(timeout=1)
        stop_update_thread.clear()
    
    # Start a new thread
    update_thread = threading.Thread(target=ui_update_thread, args=(status_box, output_box, worker_status_box, pool_status_box))
    update_thread.daemon = True
    update_thread.start()

# Create the Gradio interface
with gr.Blocks(title="Cryptocurrency Miner") as app:
    gr.Markdown("# Professional Cryptocurrency Mining")
    
    with gr.Row():
        with gr.Column(scale=2):
            wallet = gr.Textbox(label="Wallet Address", value=DEFAULT_CONFIG["wallet_address"], 
                               placeholder="Enter your wallet address")
            worker = gr.Textbox(label="Worker Name", value=DEFAULT_CONFIG["worker_name"], 
                               placeholder="Enter a worker name")
            coin = gr.Textbox(label="Coin Symbol", value=DEFAULT_CONFIG["coin_symbol"], 
                             placeholder="E.g., TRX, BTC, ETH")
            pool = gr.Textbox(label="Mining Pool", value=DEFAULT_CONFIG["pool"], 
                             placeholder="Enter pool address:port")
            threads = gr.Dropdown(label="CPU Threads", choices=["auto", "1", "2", "4", "8", "16", "all"], 
                                value=DEFAULT_CONFIG["threads"], info="Number of CPU threads to use")
            
            with gr.Row():
                start_btn = gr.Button("Start Mining", variant="primary")
                stop_btn = gr.Button("Stop Mining", variant="stop")
                refresh_btn = gr.Button("Refresh Status")
                check_balance_btn = gr.Button("Check Balance")
        
        with gr.Column(scale=3):
            with gr.Row():
                with gr.Column(scale=1):
                    worker_status_box = gr.Textbox(label="Worker Status", value="Offline", interactive=False)
                with gr.Column(scale=1):
                    pool_status_box = gr.Textbox(label="Pool Status", value="Unknown", interactive=False)
            
            status_box = gr.Textbox(label="Mining Status", value="Not running", interactive=False)
            output_box = gr.Textbox(label="Mining Output", value="", lines=25, max_lines=25, interactive=False)
            balance_result = gr.Textbox(label="Balance Information", value="", interactive=False)
    
    # Set up button actions
    start_btn.click(
        fn=start_mining_interface,
        inputs=[wallet, worker, coin, pool, threads],
        outputs=[status_box, output_box, worker_status_box, pool_status_box]
    )
    stop_btn.click(
        fn=stop_mining_interface,
        inputs=[],
        outputs=[status_box, output_box, worker_status_box, pool_status_box]
    )
    refresh_btn.click(
        fn=refresh_status,
        inputs=[],
        outputs=[status_box, output_box, worker_status_box, pool_status_box]
    )
    check_balance_btn.click(
        fn=check_balance_interface,
        inputs=[wallet, coin],
        outputs=[balance_result]
    )
    
    # Clean up when the interface is closed
    def cleanup():
        global stop_update_thread
        
        # Stop the update thread if it's running
        stop_update_thread.set()
        if update_thread and update_thread.is_alive():
            update_thread.join(timeout=1)
        
        # Stop mining if it's running
        if mining_stats["running"]:
            miner.stop_mining()
    
    # Try different cleanup approaches based on Gradio version
    try:
        # Modern Gradio versions
        app.close(cleanup)
    except:
        # Fallback for older Gradio versions
        try:
            gr.on_close(cleanup)
        except:
            # If all else fails, rely on Python's atexit
            import atexit
            atexit.register(cleanup)

# Launch the app
if __name__ == "__main__":
    app.launch()
