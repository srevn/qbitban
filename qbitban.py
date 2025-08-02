import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from collections import defaultdict, deque

import aiohttp
from cachetools import TTLCache


class QBitClient:
	def __init__(self, url, username, password):
		self.url = url
		self.auth = {"username": username, "password": password}
		self.session = None
		self.auth_cookie = None
		self.login_failed = False
		self.connected = asyncio.Event()
		self.headers = {"Referer": url}

	async def connect(self):
		self.login_failed = False
		
		if self.session:
			await self.session.close()
		
		self.session = aiohttp.ClientSession()
		try:
			async with self.session.post(f"{self.url}/api/v2/auth/login", data=self.auth, headers=self.headers) as response:
				response_text = await response.text()
				
				if response.status == 200 and response_text == "Ok.":
					log.info("Connected to qBittorrent successfully.")
					self.auth_cookie = response.cookies.get("SID").value
					log.debug(f"Captured SID: {self.auth_cookie}")
					self.connected.set()
					self.login_failed = False
					return True
				else:
					if response_text.strip() == "Fails.":
						log.error("Login failed: Invalid credentials. Initiating shutdown...")
						self.login_failed = True
					else:
						log.error(f"Login failed: HTTP {response.status}")
						self.login_failed = False
					
					self.connected.clear()
					return False
		
		except aiohttp.ClientConnectionError as e:
			log.debug(f"Connection error: {str(e)}")
			self.connected.clear()
			self.login_failed = False
			return False
		
		except Exception as e:
			log.debug(f"Unexpected error during connect: {str(e)}")
			self.connected.clear()
			self.login_failed = False
			return False
		
		self.connected.clear()
		return False

	async def fetch(self, endpoint, params=None):
		if not self.connected.is_set():
			raise aiohttp.ClientConnectionError("Not connected to qBittorrent.")
		
		headers = {**self.headers, "Cookie": f"SID={self.auth_cookie}"}
		
		try:
			async with self.session.get(f"{self.url}/{endpoint}", params=params, headers=headers) as response:
				response.raise_for_status()
				content_type = response.headers.get("Content-Type", "")
				
				if "application/json" in content_type:
					return await response.json()
				elif "text/plain" in content_type:
					return await response.text()
				else:
					raise ValueError(f"Unexpected content type: {content_type}")
		
		except aiohttp.ClientConnectionError as e:
			log.debug(f"Connection error in fetch: {str(e)}")
			self.connected.clear()
			raise
		
		except Exception as e:
			log.debug(f"Error in fetch: {str(e)}")
			self.connected.clear()
			raise
	
	async def clear_ips(self):
		headers = {**self.headers, "Cookie": f"SID={self.auth_cookie}"}
		data = {"json": json.dumps({"banned_IPs": ""})}
		
		try:
			async with self.session.post(f"{self.url}/api/v2/app/setPreferences", data=data, headers=headers) as response:
				response.raise_for_status()
				log.info("Successfully cleared previously banned IPs.")
				return True
		
		except Exception as e:
			log.debug(f"Failed to clear banned IPs list: {str(e)}")
			return False

	async def logout(self):
		if not self.session or not self.connected.is_set():
			return False
		
		headers = {**self.headers, "Cookie": f"SID={self.auth_cookie}"}
		
		try:
			async with self.session.post(f"{self.url}/api/v2/auth/logout", headers=headers) as response:
				if response.status == 200:
					log.info("Logged out successfully.")
					self.connected.clear()
					self.auth_cookie = None
					return True
				else:
					log.error(f"Logout failed: HTTP {response.status}")
					return False
		
		except Exception as e:
			log.debug(f"Logout encountered error: {str(e)}")
			return False

	async def close(self):
		if self.session:
			await self.session.close()
			self.session = None
		
		self.connected.clear()

class PeerTracker:
	def __init__(self, client, upspeed_samples, upspeed_interval, clear_exception):
		self.client = client
		self.upspeed_samples = upspeed_samples
		self.upspeed_interval = upspeed_interval
		self.tracked_peers = TTLCache(maxsize=1000, ttl=clear_exception)

	def speed_analyzer(self, speeds, ema_weight=0.8, alpha=0.3):
		if not speeds:
			return 0
		
		try:
			speeds = list(speeds)
			sorted_speeds = sorted(speeds)
			n = len(sorted_speeds)
			
			if n < 4:
				q1, q3 = sorted_speeds[0], sorted_speeds[-1]
			else:
				q1 = sorted_speeds[n // 4]
				q3 = sorted_speeds[(3 * n) // 4]
			
			iqr = q3 - q1
			lower_bound = q1 - 1.5 * iqr
			upper_bound = q3 + 1.5 * iqr
			filtered = [s for s in speeds if lower_bound <= s <= upper_bound]
			filtered_mean = sum(filtered) / len(filtered) if filtered else 0
			
			ema = speeds[0]
			for speed in speeds[1:]:
				ema = alpha * speed + (1 - alpha) * ema
			
			combined_speed = (ema * ema_weight) + (filtered_mean * (1 - ema_weight))
			
			return combined_speed
		
		except (TypeError, IndexError) as e:
			log.debug(f"Error while analyzing speed: {str(e)}")
			return 0

	async def track_speed(self, torrent_hash):
		peer_speeds = defaultdict(lambda: deque(maxlen=self.upspeed_samples))
		try:
			initial_peers_data = await self.client.fetch("api/v2/sync/torrentPeers", {"hash": torrent_hash})
			initial_peers = initial_peers_data.get("peers", {})
			if not initial_peers:
				log.debug(f"No initial peers for torrent: {torrent_hash}")
				return {}
			
			for peer_id, peer_info in initial_peers.items():
				ip = peer_info.get("ip")
				port = peer_info.get("port", 6881)
				if (ip, port) in self.tracked_peers:
					log.debug(f"Skipping measurements for {ip}:{port} as it's listed in the exceptions.")
					continue
				
				initial_speed = peer_info.get("up_speed", 0)
				if initial_speed == 0:
					log.debug(f"Dropping peer {ip}:{port} due to null initial speed.")
					continue
				
				for i in range(self.upspeed_samples):
					try:
						peers_data = await self.client.fetch("api/v2/sync/torrentPeers", {"hash": torrent_hash})
						peers = peers_data.get("peers", {})
						if peer_id not in peers:
							log.info(f"Peer {ip}:{port} is no longer present at iteration {i}. Discarding...")
							if (ip, port) in peer_speeds:
								del peer_speeds[(ip, port)]
							break
						
						if i == 0:
							log.info(f"Tracking {ip}:{port} for torrent: {torrent_hash}")
						
						current_speed = peers[peer_id]["up_speed"]
						peer_speeds[(ip, port)].append(current_speed)
					
					except Exception as e:
						log.debug(f"Error fetching/updating peer data: {str(e)}")
						return {}
					
					await asyncio.sleep(self.upspeed_interval)
			
			return {
				peer: self.speed_analyzer(list(speeds))
				for peer, speeds in peer_speeds.items()
				if speeds
			}
		
		except Exception as e:
			log.debug(f"Error while tracking peer speeds: {str(e)}")
			return {}

class BanMonitor:
	def __init__(self, client, peer_tracker, min_seeders, excluded_tags, check_interval, upspeed_threshold, clear_exception):
		self.client = client
		self.peer_tracker = peer_tracker
		self.min_seeders = min_seeders
		self.excluded_tags = excluded_tags
		self.check_interval = check_interval
		self.upspeed_threshold = upspeed_threshold
		self.tracked_torrents = TTLCache(maxsize=1000, ttl=clear_exception)
		self.active_monitors = {}

	async def speed_limit(self):
		try:
			if not self.client.connected.is_set():
				return False
			
			speed_limit_enabled = int(await self.client.fetch("api/v2/transfer/speedLimitsMode")) == 1
			
			if speed_limit_enabled:
				upspeed_limit = int(await self.client.fetch("api/v2/transfer/uploadLimit"))
				if upspeed_limit < self.upspeed_threshold:
					log.info(f"Speed limit enabled and set to {upspeed_limit / 1024:.2f} KB/s. Pausing...")
					
					while True:
						speed_limit_enabled = int(await self.client.fetch("api/v2/transfer/speedLimitsMode")) == 1
						if not speed_limit_enabled:
							log.info("Speed limit disabled. Resuming...")
							return False
						
						upspeed_limit = int(await self.client.fetch("api/v2/transfer/uploadLimit"))
						if upspeed_limit >= self.upspeed_threshold:
							log.info(f"Speed limit raised to {upspeed_limit / 1024:.2f} KB/s. Resuming...")
							return False
						
						await asyncio.sleep(self.check_interval)
			
			return False
		
		except aiohttp.ClientConnectionError:
			log.debug("Speed limit check skipped due to disconnection.")
			return False
		
		except Exception as e:
			log.debug(f"Error checking alternative speed: {str(e)}")
			return False

	async def uploading_torrents(self):
		try:
			torrents = await self.client.fetch("api/v2/torrents/info", {"filter": "active"})
			
		except Exception as e:
			log.debug(f"Failed to get torrents list: {str(e)}")
			return
		
		for torrent in torrents:
			if not (torrent["state"] in {"uploading"} and torrent["upspeed"] > 1):
				continue
			
			torrent_hash = torrent["hash"]
			torrent_tags = torrent["tags"]
			
			if torrent_hash in self.tracked_torrents:
				continue
			
			if torrent_tags:
				tags_list = [tag.strip() for tag in torrent_tags.split(',')]
				excluded = [tag for tag in tags_list if tag in self.excluded_tags]
				
				if excluded:
					log.info(f"Excluding torrent {torrent_hash} with tags: {', '.join(excluded)}")
					self.tracked_torrents[torrent_hash] = True
					continue
			
			if torrent["num_complete"] >= self.min_seeders:
				yield torrent_hash
			
			elif torrent_hash not in self.tracked_torrents:
				log.info(f"Adding exception for {torrent_hash} with {torrent['num_complete']} "
						f"{'seeder' if torrent['num_complete'] == 1 else 'seeders'}.")
				self.tracked_torrents[torrent_hash] = True
				
				try:
					peers_data = await self.client.fetch("api/v2/sync/torrentPeers", {"hash": torrent_hash})
					peers = peers_data.get("peers", {})
					
					for peer_id, peer_info in peers.items():
						ip = peer_info.get("ip")
						port = peer_info.get("port", 6881)
						if ip and port:
							log.debug(f"Adding wider exception for {torrent_hash} with {ip}:{port}.")
							self.peer_tracker.tracked_peers[(ip, port)] = True
				
				except Exception as e:
					log.debug(f"Error fetching peers for wider exception: {str(e)}")

	async def peer_monitor(self, torrent_hash):
		try:
			peer_averages = await self.peer_tracker.track_speed(torrent_hash)
			
			for (ip, port), avg_speed in peer_averages.items():
				if 0 < avg_speed < self.upspeed_threshold:
					if await self.ban_peer(ip, port):
						log.info(f"Banned peer {ip}:{port} with average upload speed {avg_speed / 1024:.2f} KB/s.")
					else:
						log.error(f"Failed to ban peer {ip}:{port}")
				
				elif avg_speed >= self.upspeed_threshold:
					log.info(f"Adding exception for {ip}:{port} with average speed {avg_speed / 1024:.2f} KB/s.")
					self.peer_tracker.tracked_peers[(ip, port)] = True
		
		finally:
			if torrent_hash in self.active_monitors:
				del self.active_monitors[torrent_hash]

	async def ban_peer(self, ip, port):
		try:
			headers = {
				**self.client.headers,
				"Cookie": f"SID={self.client.auth_cookie}",
				"Content-Type": "application/x-www-form-urlencoded"
			}
			data = {"peers": f"{ip}:{port}"}
			
			async with self.client.session.post(f"{self.client.url}/api/v2/transfer/banPeers", data=data, headers=headers) as response:
				return response.status == 200
		
		except Exception as e:
			log.debug(f"An error occurred while sending peer data: {str(e)}")
			return False

	async def torrent_monitor(self):
		while True:
			try:
				if not self.client.connected.is_set():
					await asyncio.sleep(self.check_interval)
					continue
				
				if not await self.speed_limit():
					async for torrent_hash in self.uploading_torrents():
						
						if torrent_hash in self.active_monitors:
							if not self.active_monitors[torrent_hash].done():
								log.debug(f"Monitoring {torrent_hash} in progress")
								continue
							
							log.debug(f"Monitoring completed for {torrent_hash}")
							del self.active_monitors[torrent_hash]
						
						log.debug(f"Starting new monitoring task for {torrent_hash}")
						task = asyncio.create_task(self.peer_monitor(torrent_hash))
						self.active_monitors[torrent_hash] = task
					
				await asyncio.sleep(self.check_interval)
			
			except aiohttp.ClientConnectionError as e:
				log.debug(f"Connection error during monitoring: {str(e)}")
				self.client.connected.clear()
			
			except asyncio.CancelledError:
				raise
			
			except Exception as e:
				log.debug(f"Error during torrent monitoring: {str(e)}")

class Qbitban:
	def __init__(self, config_path):
		with open(config_path, 'r') as config_file:
			self.config = json.load(config_file)
		
		self.log_file = self.config["log_file"]
		self.clear_ips = self.config["clear_ips"]
		self.clear_interval = self.config["clear_interval"]
		self.check_interval = self.config["check_interval"]
		self.last_clearup = 0
		
		self.waiting_connection = False
		
		self.logger()
		
		self.client = QBitClient(
			url = self.config["url"],
			username = self.config["username"],
			password = self.config["password"]
		)
		
		self.peer_tracker = PeerTracker(
			client = self.client,
			upspeed_samples = self.config["upspeed_samples"],
			upspeed_interval = self.config["upspeed_interval"],
			clear_exception = self.config["clear_exception"]
		)
		
		self.ban_monitor = BanMonitor(
			client = self.client,
			peer_tracker = self.peer_tracker,
			min_seeders = self.config["min_seeders"],
			excluded_tags = self.config["excluded_tags"],
			check_interval = self.config["check_interval"],
			upspeed_threshold = self.config["upspeed_threshold"],
			clear_exception = self.config["clear_exception"]
		)
	
	def logger(self):
		try:
			log.setLevel(logging.DEBUG)
			
			format = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt = '%Y-%m-%d %H:%M:%S')
			
			file_handler = logging.FileHandler(self.log_file, mode='w')
			file_handler.setLevel(logging.INFO)
			file_handler.setFormatter(format)
			log.addHandler(file_handler)
			
			if not getattr(sys, 'frozen', False):
				console_handler = logging.StreamHandler()
				console_handler.setLevel(logging.DEBUG)
				console_handler.setFormatter(format)
				log.addHandler(console_handler)
		
		except Exception as e:
			print(f"Misconfiguration in logging setup: {str(e)}", file=sys.stderr)
			raise

	async def clearup(self):
		while True:
			if (time.time() - self.last_clearup) >= self.clear_interval:
				if self.client.connected.is_set():
					await self.client.clear_ips()
					self.last_clearup = time.time()
			
			await asyncio.sleep(self.check_interval)

	async def main(self):
		self.shutdown_event = asyncio.Event()
		
		monitor_task = None
		clearup_task = None
		
		loop = asyncio.get_running_loop()
		signals = (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT, signal.SIGHUP)
		for sig in signals:
			loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.shutdown(s)))
		
		try:
			while not self.shutdown_event.is_set():
				
				if not self.client.connected.is_set() and not self.waiting_connection:
					log.info("Waiting for connection...")
					self.waiting_connection = True
				
				if not self.client.connected.is_set():
					try:
						if await asyncio.wait_for(self.client.connect(), timeout=self.check_interval):
							
							self.waiting_connection = False
							
							if self.clear_ips:
								await self.client.clear_ips()
								self.last_clearup = time.time()
								if self.clear_interval > 0:
									clearup_task = asyncio.create_task(self.clearup())
							
							monitor_task = asyncio.create_task(self.ban_monitor.torrent_monitor())
						
						else:
							if self.client.login_failed:
								log.error("Shutting down due to invalid credentials.")
								self.shutdown_event.set()
								break
							else:
								log.debug(f"No connection. Retrying in {self.check_interval}s...")
								await asyncio.sleep(self.check_interval)
								continue
					
					except asyncio.TimeoutError:
						log.warning("Connection attempt timed out. Retrying...")
						await asyncio.sleep(self.check_interval)
						continue
					
					except Exception as e:
						log.error(f"Shutting down due to error: {str(e)}")
						self.shutdown_event.set()
						break
				
				try:
					await asyncio.wait_for(self.shutdown_event.wait(), timeout=self.check_interval)
				
				except asyncio.TimeoutError:
					if not self.client.connected.is_set():
						log.error("Connection lost...")
						self.waiting_connection = False
						
						if monitor_task:
							monitor_task.cancel()
							try:
								await monitor_task
							
							except asyncio.CancelledError:
								log.debug("Monitoring task cancelled...")
							
							monitor_task = None
		
		except Exception as e:
			log.critical(f"Critical error: {str(e)}", exc_info=True)
			raise
		
		finally:
			for task in [monitor_task, clearup_task]:
				if task:
					task.cancel()
					try:
						await task
					
					except asyncio.CancelledError:
						pass
				
			await self.client.logout()
			await self.client.close()
			
			log.info("Shutdown complete.")

	async def shutdown(self, sig):
		log.debug(f"Received exit signal {sig.name}...")
		self.shutdown_event.set()

log = logging.getLogger()
if __name__ == "__main__":
	try:
		parser = argparse.ArgumentParser(description="qBitban configuration")
		parser.add_argument("--config", type=str, required=True)
		args = parser.parse_args()
		
		log.debug("Starting application...")
		qbitban = Qbitban(args.config)
		asyncio.run(qbitban.main(), debug=False)
	
	except Exception as e:
		log.error(f"Application error: {str(e)}", exc_info=True)
		sys.exit(1)
