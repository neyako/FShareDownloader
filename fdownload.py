
from __future__ import print_function

#from apiclient import errors
from colorama import init,Fore,Back,Style
from termcolor import colored
from tqdm import tqdm
from requests.exceptions import HTTPError, RequestException, Timeout

import configparser
import httpx
import json
import os
import sys
import requests
import re
from urllib.parse import parse_qs, urlparse

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
        self.token = ""
        self.session_id = ""
        self.api = httpx.Client(
            http2=True,
            timeout=30.0,
            headers={"User-Agent": "okhttp/3.6.0"},
        )
        self.web = requests.Session()
        self.web.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
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
        csrf = re.search(r'name="_csrf-app" value="([^"]+)"', form.group(0))
        linkcode = re.search(r'name="linkcode" value="([^"]+)"', form.group(0))
        if not csrf or not linkcode:
            raise FShareAPIError("Could not read Fshare download form fields")

        payload = {
            "_csrf-app": csrf.group(1),
            "linkcode": linkcode.group(1),
            "ushare": "",
            "withFcode5": "0",
        }
        try:
            response = self.web.post(
                f"{FShare_Web_URL}/download/get",
                data=payload,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": url,
                },
                timeout=30,
            )
        except RequestException as e:
            raise FShareAPIError(f"Could not create web download session: {e}")
        data = self._requests_json(response, "web download session")
        if "url" not in data:
            message = data.get("message") or data.get("errors") or data
            raise FShareAPIError(f"Could not create web download link: {message}")
        return data["url"]

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
        try:
            download_url = service.download(FShare_File_URL+fileInfo['linkcode'])
        except FShareAPIError as e:
            print(colored('Could not create download link: {}'.format(e),'red'))
            exit(1)
        download_file(download_url,location,fileInfo['name'])
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
                download_url = service.download(FShare_File_URL+fileInfo['linkcode'])
            except FShareAPIError as e:
                print(colored('Could not create download link for {}: {}'.format(fileInfo['name'], e),'red'))
                continue
            download_file(download_url,location,fileInfo['name'])
    
def download_file(url, location,filename):
    """
    Download a particular file from with direct link provided from service payload with download bar
    """
    # local_filename = url.split('/')[-1]
    local_filename = filename
    local_filename = no_accent_vietnamese(local_filename)
    local_path = location + local_filename

    try:
        with requests.get(url, stream=True,timeout=(10,30)) as r:
            try:
                r.raise_for_status()
                total_size = int(r.headers.get('content-length') or 0)
                if os.path.exists(local_path):
                    if total_size > 0 and os.path.getsize(local_path) == total_size:
                        print('Local File Existed ! Ignore downloading')
                        return 1
                    print('Local file incomplete ! Re-download')
                    os.remove(local_path)
                if (total_size > (2*1024*1024*1024)):
                #File is greater than 2Gb, use bigger chunk size
                    download_chunk_size = 2*1024*1024
                else:
                    download_chunk_size = 1024*1024
                downloaded_chunk = 0
                progressbar = tqdm(total=total_size or None,desc="Downloading",ncols=70, unit_scale=True, unit="B")
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=download_chunk_size):
                        if chunk: # filter out keep-alive new chunks
                            f.write(chunk)
                            f.flush()
                            progressbar.update(len(chunk))
                            # progressbar.update(float((downloaded_chunk*(download_chunk_size)/total_size)))
                            # downloaded_chunk += 1
                    progressbar.close()
            except HTTPError:
                print("HTTP Error")
        return (location + local_filename)
    except Timeout:
        print('Please check Internet connection, the request timed out')
    except RequestException as e:
        print('Download failed: {}'.format(e))
        
        
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
