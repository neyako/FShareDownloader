
from __future__ import print_function

#from apiclient import errors
from colorama import init,Fore,Back,Style
from termcolor import colored
from tqdm import tqdm
from requests.exceptions import HTTPError, RequestException, Timeout

import configparser
import gc
import httpx
import json
import os
import sys
import requests
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse
from requests.adapters import HTTPAdapter

# Requires: httpx[http2] requests colorama termcolor tqdm
# Debian package: python3-lxml
# libxml2 libxslt

# Main FShare URLs
FShare_File_URL = 'https://www.fshare.vn/file/'
FShare_Folder_URL = 'https://www.fshare.vn/folder/'
FShare_Web_URL = 'https://www.fshare.vn'
FShare_API_URL = 'https://api.fshare.vn/api'
re_folder_pattern = r"(https://www\.fshare\.vn/folder/)([^\?]+)(?:(\?.*))?"
re_folder_name_pattern = r"(.*/)(.*)"
File_Indicator = '/file/'
Folder_Indicator = '/folder/'
Folder_Reference_Filename = "Folder_Information.txt"
FShare_App_Key = "L2S7R6ZMagggC5wWkQhX2+aDi467PPuftWUMRFSn"

CONFIG_FILE = "credentials.ini"
TRAFFIC_CHECK_INTERVAL_SECONDS = 10 * 60
LOW_SPEED_THRESHOLD_BYTES = 256 * 1024
LOW_SPEED_GRACE_SECONDS = 120
LOW_SPEED_WINDOW_SECONDS = 60

service=''

class FShareAPIError(Exception):
    pass


class FSAPI:
    """
    Small Fshare client for the endpoints this downloader needs.

    The old get_fshare fileops metadata endpoints now often return HTML/empty
    responses. The web v3 metadata endpoint still returns JSON, while the old
    HTTP/2 session/download endpoint still creates the direct download link.
    """

    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.cookie_file = None
        self.web_download = False
        self.download_traffic = None
        self.token = ""
        self.session_id = ""
        self.api = httpx.Client(
            http2=True,
            timeout=30.0,
            headers={"User-Agent": "okhttp/3.6.0"},
        )
        self.web = requests.Session()
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2, pool_block=True)
        self.web.mount("https://", adapter)
        self.web.mount("http://", adapter)
        self.web.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Encoding": "identity",
            "Connection": "close",
        })

    def use_cookie_file(self, cookie_file):
        self.cookie_file = os.path.expanduser(cookie_file.strip())

    def login(self):
        if self.cookie_file:
            self._load_web_cookies(self.cookie_file)
            self._check_web_login()
            self.web_download = True
            return {"code": 200, "msg": "Logged in with web cookies"}

        payload = {
            "user_email": self.email,
            "password": self.password,
            "app_key": FShare_App_Key,
        }
        response = self.api.post(f"{FShare_API_URL}/user/login/", json=payload)
        data = self._httpx_json(response, "login")
        if data.get("code") != 200:
            raise FShareAPIError(data.get("msg", "Login failed"))

        self.token = data["token"]
        self.session_id = data["session_id"]
        self.api.cookies.set("session_id", self.session_id, domain="api.fshare.vn")
        return data

    def download(self, url, password=None):
        url = self.check_valid(url)
        if self.web_download:
            return self._web_download(url)

        payload = {"token": self.token, "url": url}
        if password:
            payload["password"] = password

        response = self.api.post(f"{FShare_API_URL}/session/download", json=payload)
        data = self._httpx_json(response, "download session")
        if response.status_code == 403:
            raise FShareAPIError("Password invalid")
        if response.status_code != 200 or "location" not in data:
            raise FShareAPIError(data.get("msg", "Could not create download link"))
        return data["location"]

    def get_file_info(self, url):
        linkcode = self._linkcode_from_url(url)
        data = self._get_v3_linkcode(linkcode)
        info = data.get("current")
        if not info:
            raise FShareAPIError("Could not find file information")
        return self._normalize_item(info)

    def get_folder_urls(self, url):
        linkcode = self._linkcode_from_url(url)
        items = []
        page = 1
        last_page = 1

        while page <= last_page:
            data = self._get_v3_linkcode(linkcode, page=page)
            items.extend(self._normalize_item(item) for item in data.get("items", []))
            last_page = max(last_page, self._last_page_from_links(data.get("_links", {})))
            page += 1

        return items

    def check_valid(self, url):
        parsed = urlparse(url)
        if parsed.netloc not in ("www.fshare.vn", "fshare.vn"):
            raise FShareAPIError("Must be Fshare url")
        clean_path = parsed.path.rstrip("/")
        return f"{FShare_Web_URL}{clean_path}"

    def _get_v3_linkcode(self, linkcode, page=1):
        response = self.web.get(
            f"{FShare_Web_URL}/api/v3/files/folder",
            params={
                "linkcode": linkcode,
                "sort": "type,name",
                "page": page,
                "per-page": 50,
                "details": "true",
            },
            timeout=30,
        )
        data = self._requests_json(response, "metadata lookup")
        if response.status_code != 200:
            message = data.get("message") or data.get("name") or "Metadata lookup failed"
            raise FShareAPIError(f"{message} ({response.status_code})")
        return data

    def _web_download(self, url):
        try:
            response = self.web.get(url, timeout=30)
            response.raise_for_status()
        except RequestException as e:
            raise FShareAPIError(f"Could not open Fshare download page: {e}")
        form = re.search(r'<form id="form-download".*?</form>', response.text, re.S)
        if not form:
            raise FShareAPIError("Could not find Fshare download form. Cookie may be expired.")
        form_html = form.group(0)
        payload = self._form_payload(form_html)
        csrf = payload.get("_csrf-app")
        linkcode = payload.get("linkcode") or payload.get("linkcodeDownload")
        if not csrf or not linkcode:
            raise FShareAPIError("Could not read Fshare download form fields")

        payload["linkcode"] = linkcode
        payload.setdefault("ushare", "")
        payload["withFcode5"] = "0"
        action = re.search(r'action="([^"]+)"', form_html)
        download_endpoint = urljoin(FShare_Web_URL, action.group(1)) if action else f"{FShare_Web_URL}/download/get"
        data = self._post_web_download(download_endpoint, url, payload)
        if data.get("policydowload") and "url" not in data:
            payload["slow_download"] = "1"
            data = self._post_web_download(download_endpoint, url, payload)
        if "url" not in data:
            message = data.get("message") or data.get("errors") or data
            raise FShareAPIError(f"Could not create web download link: {message}")
        return data["url"]

    def _post_web_download(self, endpoint, referer, payload):
        try:
            response = self.web.post(
                endpoint,
                data=payload,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": referer,
                },
                timeout=30,
            )
        except RequestException as e:
            raise FShareAPIError(f"Could not create web download session: {e}")
        return self._requests_json(response, "web download session")

    def _form_payload(self, form_html):
        payload = {}
        for field in re.finditer(r'<input\b[^>]*>', form_html, re.S):
            name = re.search(r'\bname="([^"]+)"', field.group(0))
            if not name:
                continue
            value = re.search(r'\bvalue="([^"]*)"', field.group(0))
            payload[name.group(1)] = value.group(1) if value else ""
        return payload

    def _load_web_cookies(self, cookie_file):
        with open(cookie_file) as f:
            data = json.load(f)
        cookies = data.get("cookies", data) if isinstance(data, dict) else data
        if not isinstance(cookies, list):
            raise FShareAPIError("Cookie file must be a JSON list or contain a cookies list")
        for cookie in cookies:
            if "name" not in cookie or "value" not in cookie:
                continue
            self.web.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )

    def _check_web_login(self):
        response = self.web.get(f"{FShare_Web_URL}/account/profile", timeout=30)
        response.raise_for_status()
        if "LoginForm[email]" in response.text or "/site/login" in response.url:
            raise FShareAPIError("Fshare cookie is expired or not logged in")

    def get_download_traffic(self):
        if not self.web_download:
            return None
        response = self.web.get(f"{FShare_Web_URL}/account/inforesource", timeout=30)
        response.raise_for_status()
        if "LoginForm[email]" in response.text or "/site/login" in response.url:
            raise FShareAPIError("Fshare cookie is expired or not logged in")
        chart = re.search(
            r"Highcharts\.chart\('container-traffic-download'.*?"
            r"\['Còn khả dụng',\s*(\d+)\],\s*\['Đã sử dụng',\s*(\d+)\]",
            response.text,
            re.S,
        )
        if chart:
            remaining_bytes = int(chart.group(1))
            used_bytes = int(chart.group(2))
            total_bytes = used_bytes + remaining_bytes
            return {
                "used": self._bytes_to_traffic(used_bytes),
                "total": self._bytes_to_traffic(total_bytes),
                "remaining": self._bytes_to_traffic(remaining_bytes),
                "percent": (used_bytes / total_bytes * 100) if total_bytes else 0,
                "used_bytes": used_bytes,
                "total_bytes": total_bytes,
                "remaining_bytes": remaining_bytes,
            }
        match = re.search(
            r'<li class="mdc-list-item download-traffic"[^>]*>.*?<p>\s*'
            r'<a[^>]*>.*?</a>\s*([^<]+?)\s*/\s*([^<]+?)\s*</p>.*?'
            r'scaleX\(([^)]+)\)',
            response.text,
            re.S,
        )
        if not match:
            return None
        used = " ".join(match.group(1).split())
        total = " ".join(match.group(2).split())
        ratio = float(match.group(3))
        return {
            "used": used,
            "total": total,
            "remaining": self._traffic_remaining(used, total),
            "percent": ratio * 100,
            "used_bytes": self._traffic_to_bytes(used),
            "total_bytes": self._traffic_to_bytes(total),
            "remaining_bytes": self._traffic_remaining_bytes(used, total),
        }

    def _traffic_remaining(self, used, total):
        remaining_bytes = self._traffic_remaining_bytes(used, total)
        if remaining_bytes is None:
            return None
        return self._bytes_to_traffic(remaining_bytes)

    def _traffic_remaining_bytes(self, used, total):
        used_bytes = self._traffic_to_bytes(used)
        total_bytes = self._traffic_to_bytes(total)
        if used_bytes is None or total_bytes is None:
            return None
        return max(total_bytes - used_bytes, 0)

    def refresh_download_traffic(self):
        self.download_traffic = self.get_download_traffic()
        return self.download_traffic

    def apply_download_traffic_usage(self, downloaded_bytes):
        if not self.download_traffic or "remaining_bytes" not in self.download_traffic:
            return
        used_bytes = (self.download_traffic.get("used_bytes") or 0) + downloaded_bytes
        total_bytes = self.download_traffic.get("total_bytes") or used_bytes
        remaining_bytes = max(total_bytes - used_bytes, 0)
        self.download_traffic.update({
            "used": self._bytes_to_traffic(used_bytes),
            "total": self._bytes_to_traffic(total_bytes),
            "remaining": self._bytes_to_traffic(remaining_bytes),
            "percent": (used_bytes / total_bytes * 100) if total_bytes else 0,
            "used_bytes": used_bytes,
            "total_bytes": total_bytes,
            "remaining_bytes": remaining_bytes,
        })

    def is_download_traffic_low(self, file_size):
        if not self.download_traffic or file_size <= 0:
            return False
        remaining_bytes = self.download_traffic.get("remaining_bytes")
        return remaining_bytes is not None and remaining_bytes < file_size

    def _traffic_to_bytes(self, value):
        match = re.search(r'([\d.]+)\s*([KMGT]?B)', value, re.I)
        if not match:
            return None
        units = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
        return float(match.group(1)) * units[match.group(2).upper()]

    def _bytes_to_traffic(self, value):
        for unit in ("TB", "GB", "MB", "KB"):
            factor = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}[unit]
            if value >= factor:
                return "{:.1f} {}".format(value / factor, unit)
        return "{} B".format(int(value))

    def _normalize_item(self, item):
        normalized = dict(item)
        item_type = int(normalized.get("type", normalized.get("file_type", 1)))
        normalized["file_type"] = item_type
        normalized["furl"] = (
            FShare_Folder_URL if item_type == 0 else FShare_File_URL
        ) + normalized["linkcode"]
        return normalized

    def _linkcode_from_url(self, url):
        parsed = urlparse(url)
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 2 and path_parts[0] in ("file", "folder"):
            return path_parts[1]
        if not parsed.netloc and url.strip():
            return url.strip().split("?")[0].strip("/")
        raise FShareAPIError("Could not read Fshare linkcode from URL")

    def _last_page_from_links(self, links):
        last = links.get("last") or links.get("self") or ""
        page = parse_qs(urlparse(last).query).get("page", ["1"])[0]
        try:
            return int(page)
        except ValueError:
            return 1

    def _httpx_json(self, response, action):
        try:
            return response.json()
        except ValueError:
            snippet = response.text.strip().replace("\n", " ")[:200]
            raise FShareAPIError(
                f"Fshare {action} returned non-JSON response "
                f"({response.status_code} {response.headers.get('content-type')}): "
                f"{snippet or '<empty>'}"
            )

    def _requests_json(self, response, action):
        try:
            return response.json()
        except ValueError:
            snippet = response.text.strip().replace("\n", " ")[:200]
            raise FShareAPIError(
                f"Fshare {action} returned non-JSON response "
                f"({response.status_code} {response.headers.get('content-type')}): "
                f"{snippet or '<empty>'}"
            )


def main():
    splash_screen()
    print(colored('Loading configuration...','white'))
    login_credential = configuration_read(CONFIG_FILE)
    print(colored('Login...','white'))
    login_status = perform_login(login_credential)

    if login_status:
        print(colored('Logged in successfully!','white'))
        print_download_traffic()
    else:
        print(colored('Login failed! Please check login credentials in {}'.format(CONFIG_FILE),'white'))
        print(colored('Quit','white'))
        exit()

    if(len(sys.argv))<3:
        # Require user's inputs to to download / ID and location
        
        location = input("Local Path (Enter . for current directory): ")
        while not location:
            location = input("Local Path (Enter . for current directory): ")
        if location[-1] != '/':
            location += '/'
                
        downloadID = input("FShare Folder/File URL: ")
        while not downloadID:
            downloadID = input("FShare Folder/File URL: ")
        
    else:
        downloadID = sys.argv[1]
        location = sys.argv[2]
        if location[-1] != '/':
            location += '/'
    
    # Check file or folder
    if not is_folder(downloadID):
        # It's a file, download it now
        try:
            fileInfo = service.get_file_info(downloadID)
        except FShareAPIError as e:
            print(colored('Could not get file information: {}'.format(e),'red'))
            exit(1)
        print(colored('File: ','white'),colored('{}'.format(fileInfo['name']),'yellow'))
        # print(f'Saving to {location}')
        file_url = FShare_File_URL+fileInfo['linkcode']
        try:
            download_file(file_url,location,fileInfo['name'],fileInfo.get('size') or fileInfo.get('file_size'))
        except FShareAPIError as e:
            print(colored('Could not create download link: {}'.format(e),'red'))
            exit(1)
        print(colored('{} downloaded'.format(fileInfo['name']),'yellow'))

    elif is_folder(downloadID):
        # It's folder, explode and download
        print("Folder detected, recursively download folder")
        download_folder(downloadID,location)
    else:
        print('Error, unknown link')

                
    splash_screen_end()
    exit(0)

def download_folder(url, location):
    """
    Download whole fshare folder / a local folder name with folderID being read from URL will be created if it doesn't exist
    """
    match = re.search(re_folder_pattern,url)
    if match is not None:
        folderID = match.group(2)
    else:
        print(colored("Folder Link error, please make sure it's welformed as https://www.fshare.vn/folder/XXXXXXXXXX (token trail is optional)",'red'))
        return 1
    
    try:
        folderList = service.get_folder_urls(url)
    except FShareAPIError as e:
        print(colored("Could not get folder information: {}".format(e),'red'))
        return 1
    if (len(folderList)) < 1:
        print("Folder empty!")
        return 1

    if not os.path.exists(location + folderID):
        os.makedirs(location + folderID)
    # Update new location to new directory
    location += folderID + "/"

    
    # detect and count sub-folders / we will ignore subfolder
    sub_folder_count = 0
    for fileInfo in folderList:
        if is_folder(fileInfo['furl']):
            sub_folder_count += 1
    if sub_folder_count > 0:
        print(colored("We found {} file(s) and {} sub-folder(s) in the link, we're skipping folder, ONLY FILES will be downloaded!".format(len(folderList)-sub_folder_count,sub_folder_count),'yellow'))

    # We will try to detect the folder name and write it down to the file for reference later
    # Fshare store the name including the parent folder so we have to regex to match

    match = re.search(re_folder_name_pattern,folderList[0]['path'])
    with open(location + Folder_Reference_Filename,"w") as f:
        f.writelines(f"Folder URL: {url} \n")
        f.writelines(f"Folder name: {match.group(2)} \n")
        f.writelines(f"File count: {len(folderList)-sub_folder_count} \n")
        f.writelines(f"Sub-Folder count: {sub_folder_count} \n")

        
    total_file_count = len(folderList)-sub_folder_count
    print(colored("We found {} file(s) in the link, download them now".format(total_file_count),'yellow'))
    # loop through whole directory and download
    fileCount = 0
    for fileInfo in folderList:
        if not is_folder(fileInfo['furl']):
            fileCount += 1
            print(colored('File #{}/{}: '.format(fileCount,total_file_count),'white'),colored('{}'.format(fileInfo['name']),'yellow'))
            try:
                download_file(FShare_File_URL+fileInfo['linkcode'],location,fileInfo['name'],fileInfo.get('size') or fileInfo.get('file_size'))
            except FShareAPIError as e:
                print(colored('Could not create download link for {}: {}'.format(fileInfo['name'], e),'red'))
                continue

def print_download_traffic():
    try:
        traffic = service.refresh_download_traffic()
    except (FShareAPIError, requests.RequestException):
        return
    if not traffic:
        return
    remaining = traffic.get("remaining") or "unknown"
    print(colored('Download traffic today: ', 'white'), colored(
        '{} / {} used ({:.1f}%), {} remaining'.format(
            traffic["used"],
            traffic["total"],
            traffic["percent"],
            remaining,
        ),
        'yellow',
    ))
    
def wait_for_download_traffic(file_size, force_wait=False):
    if not getattr(service, "web_download", False) or file_size <= 0:
        return
    while force_wait or service.is_download_traffic_low(file_size):
        remaining = service.download_traffic.get("remaining") if service.download_traffic else "unknown"
        print(colored(
            'Fshare daily traffic low ({} left, need {}). Pausing 10 minutes before checking again.'.format(
                remaining,
                service._bytes_to_traffic(file_size),
            ),
            'yellow',
        ))
        time.sleep(TRAFFIC_CHECK_INTERVAL_SECONDS)
        force_wait = False
        try:
            traffic = service.refresh_download_traffic()
        except (FShareAPIError, requests.RequestException) as e:
            print(colored('Could not refresh Fshare traffic: {}'.format(e), 'yellow'))
            continue
        finally:
            service.web.close()
            gc.collect()
        if traffic:
            print(colored('Download traffic now: ', 'white'), colored(
                '{} / {} used ({:.1f}%), {} remaining'.format(
                    traffic["used"],
                    traffic["total"],
                    traffic["percent"],
                    traffic["remaining"],
                ),
                'yellow',
            ))

def download_file(file_url, location,filename, expected_size=None):
    """
    Download a particular file from with direct link provided from service payload with download bar
    """
    # local_filename = url.split('/')[-1]
    local_filename = filename
    local_filename = no_accent_vietnamese(local_filename)
    local_path = location + local_filename
    expected_size = normalize_file_size(expected_size)

    if expected_size and os.path.exists(local_path):
        current_size = os.path.getsize(local_path)
        if current_size == expected_size:
            print('Local File Existed ! Ignore downloading')
            return 1
        if current_size > 0:
            print('Local file incomplete ! Resume downloading')
        else:
            print('Local file incomplete ! Re-download')

    download_session = service.web if getattr(service, "web_download", False) else requests

    while True:
        download_url = service.download(file_url)
        local_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        resume_from = local_size if expected_size and local_size < expected_size else 0
        headers = {
            "Accept-Encoding": "identity",
            "Connection": "close",
            "Range": "bytes={}-".format(resume_from),
        }

        try:
            with download_session.get(download_url, stream=True, timeout=(10,30), headers=headers) as r:
                if resume_from > 0 and r.status_code != 206:
                    print('Server did not resume partial file. Re-download')
                    os.remove(local_path)
                    expected_size = 0
                    resume_from = 0
                    r.close()
                    continue
                r.raise_for_status()
                total_size = total_size_from_response(r, resume_from)
                if total_size:
                    expected_size = total_size
                if os.path.exists(local_path):
                    current_size = os.path.getsize(local_path)
                    if expected_size > 0 and current_size >= expected_size:
                        print('Local File Existed ! Ignore downloading')
                        return 1
                    if resume_from == 0 and current_size > 0 and expected_size > 0:
                        r.close()
                        continue
                    if current_size > 0:
                        print('Local file incomplete ! Resume downloading')
                    elif resume_from == 0:
                        print('Local file incomplete ! Re-download')
                remaining_size = expected_size - resume_from if expected_size else 0
                if getattr(service, "web_download", False) and service.is_download_traffic_low(remaining_size):
                    r.close()
                    wait_for_download_traffic(remaining_size)
                    continue
                if (total_size > (2*1024*1024*1024)):
                #File is greater than 2Gb, use bigger chunk size
                    download_chunk_size = 8*1024*1024
                else:
                    download_chunk_size = 4*1024*1024
                downloaded_bytes = 0
                window_bytes = 0
                started_at = time.monotonic()
                window_started_at = started_at
                progressbar = tqdm(
                    total=total_size or None,
                    initial=resume_from if total_size else 0,
                    desc="Downloading",
                    ncols=70,
                    unit_scale=True,
                    unit="B",
                )
                try:
                    with open(local_path, 'ab' if resume_from > 0 else 'wb') as f:
                        for chunk in r.iter_content(chunk_size=download_chunk_size):
                            if chunk: # filter out keep-alive new chunks
                                f.write(chunk)
                                chunk_size = len(chunk)
                                downloaded_bytes += chunk_size
                                window_bytes += chunk_size
                                progressbar.update(chunk_size)
                                now = time.monotonic()
                                if now - window_started_at >= LOW_SPEED_WINDOW_SECONDS:
                                    speed = window_bytes / (now - window_started_at)
                                    elapsed = now - started_at
                                    if (
                                        getattr(service, "web_download", False)
                                        and elapsed >= LOW_SPEED_GRACE_SECONDS
                                        and speed < LOW_SPEED_THRESHOLD_BYTES
                                    ):
                                        print(colored(
                                            'Download speed too low ({}/s). Pausing and checking Fshare quota every 10 minutes.'.format(
                                                service._bytes_to_traffic(speed),
                                            ),
                                            'yellow',
                                        ))
                                        if downloaded_bytes:
                                            service.apply_download_traffic_usage(downloaded_bytes)
                                        remaining_size = total_size - os.path.getsize(local_path) if total_size else 0
                                        wait_for_download_traffic(remaining_size, force_wait=True)
                                        break
                                    window_bytes = 0
                                    window_started_at = now
                        else:
                            if getattr(service, "web_download", False):
                                service.apply_download_traffic_usage(downloaded_bytes)
                                if service.download_traffic:
                                    print(colored('Download traffic left: ', 'white'), colored(
                                        '{} remaining'.format(service.download_traffic["remaining"]),
                                        'yellow',
                                    ))
                            return (location + local_filename)
                finally:
                    progressbar.close()
                continue
        except HTTPError:
            print("HTTP Error")
            return None
        except Timeout:
            print('Please check Internet connection, the request timed out')
            return None
        except RequestException as e:
            print('Download failed: {}'.format(e))
            return None

def total_size_from_response(response, resume_from):
    content_range = response.headers.get('content-range')
    if content_range:
        match = re.search(r'/(\d+)$', content_range)
        if match:
            return int(match.group(1))
    content_length = int(response.headers.get('content-length') or 0)
    return resume_from + content_length if resume_from else content_length

def normalize_file_size(value):
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    match = re.search(r'([\d.]+)\s*([KMGT]?B)', value, re.I)
    if not match:
        return 0
    units = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return int(float(match.group(1)) * units[match.group(2).upper()])

def is_folder(url):
    if (url.find(Folder_Indicator)) != -1:
        return True
    else:
        return False

def perform_login(login_credential):
    """
    Perform login and return the status True/False
    """    
    global service
    service = FSAPI(login_credential.get('username',''),login_credential.get('password',''))
    if login_credential.get('cookie_file'):
        service.use_cookie_file(login_credential.get('cookie_file'))
    login_status = True
    try:
        if not service.cookie_file and (not service.email or not service.password):
            raise FShareAPIError("Missing username/password or cookie_file")
        service.login()
    except (KeyError, FShareAPIError, httpx.HTTPError, requests.RequestException, OSError):
        login_status=False
    return login_status

def configuration_read(filename):
    """
    Read configuration from file / without header section
    """
    config = configparser.ConfigParser()
    # Append a header section to avoid header section in the configuration file
    # config.read(filename,encoding="utf-8")
    with open(filename) as f:
        config.read_string('[DEFAULT]\n'+f.read())
    return config['DEFAULT']


def cls():
    os.system('cls' if os.name=='nt' else 'clear')

def splash_screen():
# use Colorama to make Termcolor work on Windows too
    init()
    # now, to clear the screen
    cls()


    print(colored(r'_______      _______. __    __       ___      .______       _______     ____    ____ .__   __.                     ', 'red'))
    print(colored(r'|   ____|    /       ||  |  |  |     /   \     |   _  \     |   ____|    \   \  /   / |  \ |  |                    ', 'red'))
    print(colored(r'|  |__      |   (----`|  |__|  |    /  ^  \    |  |_)  |    |  |__        \   \/   /  |   \|  |                    ', 'red'))
    print(colored(r'|   __|      \   \    |   __   |   /  /_\  \   |      /     |   __|        \      /   |  . `  |                    ', 'yellow'))
    print(colored(r'|  |     .----)   |   |  |  |  |  /  _____  \  |  |\  \----.|  |____  __    \    /    |  |\   |                    ', 'yellow'))
    print(colored(r'|__|     |_______/    |__|  |__| /__/     \__\ | _| `._____||_______|(__)    \__/     |__| \__|                    ', 'green'))
    print(colored(r'_______   ______   ____    __    ____ .__   __.  __        ______        ___       _______   _______ .______       ', 'blue'))
    print(colored(r'|       \ /  __  \  \   \  /  \  /   / |  \ |  | |  |      /  __  \      /   \     |       \ |   ____||   _  \     ', 'blue'))
    print(colored(r'|  .--.  |  |  |  |  \   \/    \/   /  |   \|  | |  |     |  |  |  |    /  ^  \    |  .--.  ||  |__   |  |_)  |    ', 'magenta'))
    print(colored(r'|  |  |  |  |  |  |   \            /   |  . `  | |  |     |  |  |  |   /  /_\  \   |  |  |  ||   __|  |      /     ', 'magenta'))
    print(colored(r"|  '--'  |  `--'  |    \    /\    /    |  |\   | |  `----.|  `--'  |  /  _____  \  |  '--'  ||  |____ |  |\  \----.", 'cyan'))
    print(colored(r'|_______/ \______/      \__/  \__/     |__| \__| |_______| \______/  /__/     \__\ |_______/ |_______|| _| `._____|', 'cyan'))
    print(colored('===================================================================================================================', 'white'))
    print(colored('                                                                             Version : ', 'yellow'), (1.0))
    print(colored('                                                                              Author : ', 'yellow'), ('haind'))
    print(colored('                                        Github : ', 'yellow'), ('https://github.com/haindvn/FShareDownloader'))
    print(colored('===================================================================================================================', 'white'))

def splash_screen_end():
    print(colored('===================================================================================================================', 'white'))
    print(colored('Download Finished','green'))

def no_accent_vietnamese(s):
    #s = s.decode('utf-8', errors='ignore')
    s = re.sub(u'[àáạảãâầấậẩẫăằắặẳẵ]', 'a', s)
    s = re.sub(u'[ÀÁẠẢÃĂẰẮẶẲẴÂẦẤẬẨẪ]', 'A', s)
    s = re.sub(u'[èéẹẻẽêềếệểễ]', 'e', s)
    s = re.sub(u'[ÈÉẸẺẼÊỀẾỆỂỄ]', 'E', s)
    s = re.sub(u'[òóọỏõôồốộổỗơờớợởỡ]', 'o', s)
    s = re.sub(u'[ÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠ]', 'O', s)
    s = re.sub(u'[ìíịỉĩ]', 'i', s)
    s = re.sub(u'[ÌÍỊỈĨ]', 'I', s)
    s = re.sub(u'[ùúụủũưừứựửữ]', 'u', s)
    s = re.sub(u'[ƯỪỨỰỬỮÙÚỤỦŨ]', 'U', s)
    s = re.sub(u'[ỳýỵỷỹ]', 'y', s)
    s = re.sub(u'[ỲÝỴỶỸ]', 'Y', s)
    s = re.sub(u'[Đ]', 'D', s)
    s = re.sub(u'[đ]', 'd', s)
    return s

if __name__ == '__main__':
    main()

#fileinfo = bot.get_file_info(URL)
#print("Direct Link:",bot.download("https://www.fshare.vn/file/{}".format(fileinfo['linkcode'])))
