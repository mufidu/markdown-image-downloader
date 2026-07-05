import io
import logging
import mimetypes
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.error import HTTPError, URLError

from PIL import Image
from tqdm import tqdm


class ImageDownloader:
    def __init__(self, folder_name: str):
        self.folder_name = Path(folder_name)
        self.attachment_dir_name = "_attachments"
        self.attachments_dir = self.folder_name / self.attachment_dir_name
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
        }
        self.timeout = 30
        self.setup_logging()

    def setup_logging(self) -> None:
        """Configure logging settings."""
        script_dir = Path(__file__).parent
        log_dir = script_dir / "logs"
        log_dir.mkdir(exist_ok=True)

        log_file = log_dir / "image_downloader.log"

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.FileHandler(log_file, mode="w"),  # Overwrite log file each run
                logging.StreamHandler(),
            ],
        )
        logging.info(f"Logging to: {log_file}")

    def unwrap_cdn_url(self, url: str) -> str:
        """Unwrap nested target URL if it's hosted on a known image-fetching CDN proxy."""
        # Substack CDN
        if "substackcdn.com/image/fetch/" in url:
            parts = url.split("substackcdn.com/image/fetch/")
            if len(parts) > 1:
                rest = parts[1]
                rest_unquoted = urllib.parse.unquote(rest)
                for prefix in ("https://", "http://"):
                    idx = rest_unquoted.find(prefix)
                    if idx != -1:
                        return rest_unquoted[idx:]
        # Omnivore CDN
        if "proxy-prod.omnivore-image-cache.app/" in url:
            parts = url.split("proxy-prod.omnivore-image-cache.app/")
            if len(parts) > 1:
                rest = parts[1]
                rest_unquoted = urllib.parse.unquote(rest)
                for prefix in ("https://", "http://"):
                    idx = rest_unquoted.find(prefix)
                    if idx != -1:
                        return rest_unquoted[idx:]
        # Microlink CDN
        if "i.microlink.io/" in url:
            parts = url.split("i.microlink.io/")
            if len(parts) > 1:
                rest = parts[1]
                rest_unquoted = urllib.parse.unquote(rest)
                for prefix in ("https://", "http://"):
                    idx = rest_unquoted.find(prefix)
                    if idx != -1:
                        return rest_unquoted[idx:]
        return url

    def safe_quote_url(self, url: str) -> str:
        """Quote a URL without double-encoding existing %xx escape sequences."""
        pattern = re.compile(r'%[0-9a-fA-F]{2}')
        parts = []
        last_idx = 0
        for match in pattern.finditer(url):
            start, end = match.span()
            part_before = url[last_idx:start]
            quoted_before = urllib.parse.quote(part_before, safe='/:?=&@')
            parts.append(quoted_before)
            parts.append(url[start:end])
            last_idx = end
        
        part_after = url[last_idx:]
        quoted_after = urllib.parse.quote(part_after, safe='/:?=&@')
        parts.append(quoted_after)
        return "".join(parts)

    def parse_markdown_images(self, content: str) -> List[Tuple[str, str]]:
        """Parse markdown image links, handling balanced parentheses in URLs."""
        images = []
        i = 0
        n = len(content)
        while i < n:
            if content[i:i+2] == "![":
                alt_start = i + 2
                alt_end = -1
                bracket_balance = 1
                j = alt_start
                while j < n:
                    if content[j] == '\\':
                        j += 2
                        continue
                    elif content[j] == '[':
                        bracket_balance += 1
                    elif content[j] == ']':
                        bracket_balance -= 1
                        if bracket_balance == 0:
                            alt_end = j
                            break
                    j += 1
                
                if alt_end != -1 and alt_end + 1 < n and content[alt_end + 1] == '(':
                    url_start = alt_end + 2
                    url_end = -1
                    paren_balance = 1
                    k = url_start
                    while k < n:
                        if content[k] == '\\':
                            k += 2
                            continue
                        elif content[k] == '(':
                            paren_balance += 1
                        elif content[k] == ')':
                            paren_balance -= 1
                            if paren_balance == 0:
                                url_end = k
                                break
                        k += 1
                    
                    if url_end != -1:
                        alt_text = content[alt_start:alt_end]
                        url = content[url_start:url_end]
                        images.append((alt_text, url))
                        i = url_end + 1
                        continue
                i += 1
            else:
                i += 1
        return images

    def _download_with_urllib(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            return response.read()

    def _download_with_curl(self, url: str) -> bytes:
        import subprocess
        cmd = [
            "curl",
            "-sL",
            "--max-time", str(self.timeout),
            url
        ]
        result = subprocess.run(cmd, capture_output=True, check=True)
        return result.stdout


    def setup_directories(self) -> None:
        """Create necessary directories if they don't exist."""
        self.attachments_dir.mkdir(parents=True, exist_ok=True)

    def get_markdown_files(self) -> List[Path]:
        """Get all markdown files in the specified folder."""
        return list(self.folder_name.glob("*.md"))

    def is_attachment_path(self, url: str) -> bool:
        """Check if the URL is already pointing to the _attachments folder."""
        # Handle URL-encoded paths
        decoded_url = urllib.parse.unquote(url)

        # Normalize path separators
        normalized_url = decoded_url.replace("\\", "/").lower()

        # List of patterns that indicate an attachment
        attachment_patterns = [
            f"/{self.attachment_dir_name}/",  # Unix-style path
            f"\\{self.attachment_dir_name}\\",  # Windows-style path
            f"{self.attachment_dir_name}/",  # Relative path
            f"./{self.attachment_dir_name}/",  # Explicit relative path
        ]

        # Check if it's a URL or a local path
        parsed_url = urllib.parse.urlparse(normalized_url)

        # If it's a local path (no scheme or network location)
        if not parsed_url.scheme and not parsed_url.netloc:
            # Check if it matches any attachment patterns
            return any(
                pattern.lower() in normalized_url for pattern in attachment_patterns
            )

        return False

    def get_file_extension(self, url: str, content_type: str = None) -> str:
        """Determine file extension based on URL and content type."""
        # Parse the URL and get the path
        parsed_url = urllib.parse.urlparse(url)

        # Split the path to separate actual path from query parameters
        path = parsed_url.path

        # Find the last occurrence of a period in the path (before query parameters)
        ext = ""
        path_parts = path.split("/")[-1].split(".")
        if len(path_parts) > 1:
            ext = f".{path_parts[-1].lower()}"

            # Check if it's a valid image extension
            valid_image_extensions = {
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".bmp",
                ".webp",
                ".tiff",
                ".svg",
            }
            if ext not in valid_image_extensions:
                ext = ""

        # If no valid extension found in URL, try content type
        if not ext and content_type:
            ext = mimetypes.guess_extension(content_type)
            if ext:
                return ext

        # If still no extension, default to .jpg
        return ext or ".jpg"

    def compress_image(self, image_data: bytes, max_size_kb: int = 500) -> bytes:
        """Compress image while maintaining reasonable quality."""
        try:
            # Open image from bytes
            img = Image.open(io.BytesIO(image_data))
            img.load()

            # Handle transparency for various modes
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                # Convert to RGBA to ensure we have an alpha channel
                img = img.convert("RGBA")
                # Create a white background
                bg = Image.new("RGB", img.size, (255, 255, 255))
                # Paste the image using its alpha channel as a mask
                bg.paste(img, mask=img.split()[3])
                img = bg
            else:
                # For all other modes, convert directly to RGB
                img = img.convert("RGB")

            # Initial quality
            quality = 75
            output = io.BytesIO()

            # Save with progressive option for JPEGs
            img.save(
                output, format="JPEG", quality=quality, optimize=True, progressive=True
            )

            # Reduce quality until file size is under max_size_kb
            while output.tell() > max_size_kb * 1024 and quality > 20:
                output = io.BytesIO()
                quality -= 5
                img.save(
                    output,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )

            return output.getvalue()

        except Exception as e:
            logging.error(f"Error compressing image: {str(e)}")
            return image_data

    def sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to remove illegal characters."""
        # Replace illegal characters with underscore
        illegal_chars = r'[<>:"/\\|?*%]'
        sanitized = re.sub(illegal_chars, "_", filename)
        # Remove any consecutive underscores
        sanitized = re.sub(r"_+", "_", sanitized)
        # Remove leading/trailing underscores
        sanitized = sanitized.strip("_")
        return sanitized if sanitized else "image"

    def get_markdown_filename(self, img_url: str) -> str:
        """Extract and sanitize filename from URL or generate one if not present."""
        try:
            # Decode the URL first
            decoded_url = urllib.parse.unquote(img_url)

            # Parse the URL and get the path
            parsed_url = urllib.parse.urlparse(decoded_url)
            path = parsed_url.path

            # Extract filename from path, ignoring query parameters
            filename = os.path.basename(path.split("?")[0])

            if not filename:
                # If no filename in URL, use the domain name or 'image'
                filename = parsed_url.netloc.split(".")[0] or "image"

            # Sanitize the filename
            base_name, ext = os.path.splitext(filename)
            if not ext:
                ext = ".jpg"  # Default extension if none is present

            # Limit filename length, keep extension
            max_length = 100  # Set maximum length
            if len(base_name) > max_length:
                base_name = base_name[:max_length]

            # Add timestamp to ensure unique filename
            timestamp = str(time.time()).replace(".", "")
            
            # Process base name and extension separately
            sanitized_base = self.sanitize_filename(base_name)
            sanitized_ext = self.sanitize_filename(ext)

            # Ensure final filename doesn't exceed system limits
            final_filename = f"{sanitized_base}_{timestamp}{sanitized_ext}"
            if len(final_filename) > 200:  # Leave some margin
                final_filename = f"img_{timestamp}{sanitized_ext}"

            # Final check to ensure filename is valid
            final_filename = self.sanitize_filename(final_filename)
            
            return final_filename
        except Exception as e:
            logging.error(f"Error generating filename: {str(e)}")
            # If error occurs, return a safe default filename
            return f"image_{int(time.time())}.jpg"

    def download_image(self, img_url: str, file_name: str) -> Tuple[str, str, bool]:
        """Download a single image and return the new local path."""
        try:
            # Skip if already in _attachments folder
            if self.is_attachment_path(img_url):
                logging.info(
                    f"Skipping {img_url} as it's already in _attachments folder"
                )
                return img_url, img_url, True

            # Check if the URL is relative to the markdown file location
            if not urllib.parse.urlparse(img_url).scheme:
                # Convert relative path to absolute path
                absolute_path = (Path(self.folder_name) / img_url).resolve()
                if absolute_path.exists():
                    # If it's a local file, just copy it to attachments
                    with open(absolute_path, "rb") as f:
                        content = f.read()
                else:
                    raise FileNotFoundError(f"Local file not found: {absolute_path}")
            else:
                # Download from URL
                unwrapped_url = self.unwrap_cdn_url(img_url)
                encode_url = self.safe_quote_url(unwrapped_url)
                try:
                    content = self._download_with_urllib(encode_url)
                except Exception as urllib_exc:
                    is_404 = isinstance(urllib_exc, HTTPError) and urllib_exc.code == 404
                    if is_404:
                        raise urllib_exc
                    
                    logging.info(f"urllib failed for {img_url} ({str(urllib_exc)}), trying curl fallback...")
                    try:
                        content = self._download_with_curl(encode_url)
                        if not content:
                            raise Exception("curl returned empty content")
                    except Exception as curl_exc:
                        raise urllib_exc

            # Compress the image
            compressed_content = self.compress_image(content)

            # Use the same filename as in the markdown link, but sanitized
            filename = self.get_markdown_filename(img_url)

            local_path = self.attachments_dir / filename

            with open(local_path, "wb") as f:
                f.write(compressed_content)

            # Convert space to %20 in the filename
            # This is necessary for the image to display in the markdown preview
            filename = filename.replace(" ", "%20")

            return img_url, f"_attachments/{filename}", True

        except (HTTPError, URLError) as e:
            logging.error(f"Failed to download {img_url} from {file_name}: {str(e)}")
            return img_url, "", False
        except Exception as e:
            logging.error(
                f"Unexpected error downloading {img_url} from {file_name}: {str(e)}"
            )
            return img_url, "", False

    def get_all_images(self) -> Dict[Path, List[Tuple[str, str]]]:
        """Get all images from all markdown files."""
        files_images = {}
        for file_path in self.get_markdown_files():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                # Find all image references, handling balanced parentheses in URLs
                images = self.parse_markdown_images(content)
                if images:
                    # Filter out images that are already in _attachments
                    images = [
                        (alt, url)
                        for alt, url in images
                        if not self.is_attachment_path(url)
                    ]
                    if (
                        images
                    ):  # Only add to files_images if there are images to process
                        files_images[file_path] = images
            except Exception as e:
                logging.error(f"Error reading file {file_path}: {str(e)}")
        return files_images

    def process_files(self, files_images: Dict[Path, List[Tuple[str, str]]]) -> None:
        """Process all files and their images with a single progress bar."""
        total_images = sum(len(images) for images in files_images.values())
        if total_images == 0:
            logging.info("No new images to process")
            return

        logging.info(
            f"Found {total_images} images to process across {len(files_images)} files"
        )

        with ThreadPoolExecutor(max_workers=5) as executor:
            # Create futures for all images
            futures = []
            file_image_map = {}  # Map to track which file each future belongs to

            for file_path, images in files_images.items():
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                for img in images:
                    future = executor.submit(
                        self.download_image, img[1], file_path.name
                    )
                    futures.append(future)
                    file_image_map[future] = (file_path, content)

            # Process all downloads with a single progress bar
            replacements = {}  # file_path -> list of (old_url, new_path) tuples
            with tqdm(
                total=total_images, desc="Downloading and compressing images"
            ) as pbar:
                for future in as_completed(futures):
                    file_path, content = file_image_map[future]
                    old_url, new_path, success = future.result()

                    if success and old_url != new_path:
                        if file_path not in replacements:
                            replacements[file_path] = []
                        replacements[file_path].append((old_url, new_path))

                    pbar.update(1)
                    time.sleep(1)  # Small delay between downloads

            # Update all files with their replacements
            for file_path, replacement_list in replacements.items():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()

                    for old_url, new_path in replacement_list:
                        content = content.replace(old_url, new_path)

                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)

                except Exception as e:
                    logging.error(f"Error updating file {file_path}: {str(e)}")

    def run(self) -> None:
        """Main execution method."""
        try:
            self.setup_directories()
            files_images = self.get_all_images()

            if not files_images:
                logging.info("No new images to download")
                return

            self.process_files(files_images)

        except Exception as e:
            logging.error(f"Fatal error: {str(e)}")


def main():
    import argparse
    import importlib.metadata

    parser = argparse.ArgumentParser(
        prog="markdown-image-downloader",
        description="Download and localize images referenced in markdown files.",
    )
    parser.add_argument("folder", help="Folder containing markdown files")
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"%(prog)s {importlib.metadata.version('markdown-image-downloader')}",
    )
    args = parser.parse_args()

    downloader = ImageDownloader(args.folder)
    downloader.run()


if __name__ == "__main__":
    main()
