import os
import time
import logging
import asyncio
import shutil
import re
from typing import List, Tuple, Optional

from telethon.tl.types import InputFile, InputFileBig, DocumentAttributeFilename
from telethon.errors import FloodWaitError, MessageNotModifiedError, TimeoutError
# Note: ConnectionError and NetworkError are built-in Python exceptions, not from telethon

# Assuming config and utils.formatting are accessible
from config import DOWNLOAD_DIR, MAX_FILE_SIZE, ZIP_PART_SIZE
from utils.formatting import format_size, format_time
from utils.memory_monitor import memory_monitor, safe_large_operation, emergency_cleanup

# Try to import the compressor function, fallback if not available
try:
    from utils.compressor import stream_compress  # Import the compressor function
    HAS_COMPRESSOR = True
except ImportError as e:
    logger = logging.getLogger(__name__)
    logger.warning(f"Compressor not available: {e}. Compression features disabled.")
    HAS_COMPRESSOR = False
    
    # Define a dummy function to prevent errors
    async def stream_compress(*args, **kwargs):
        logger.error("Compression not available - zipstream package missing")
        return [], None

from models import Task
from enums import TaskStatus

logger = logging.getLogger(__name__)

# --- Progress Update Helpers ---

async def update_progress_message(client, chat_id, msg_id, current, total, filename, start_time, status_text_prefix=""):
    """Update progress message for uploads/downloads with rate limiting."""
    # Use a simple time-based debouncer per message ID
    now = time.time()
    message_key = f"progress_{chat_id}_{msg_id}"
    last_update_time = getattr(update_progress_message, message_key, 0)

    if now - last_update_time < 2.0:  # Update at most every 2 seconds
        return

    try:
        if total > 0:
            percent = current / total * 100
            elapsed_time = now - start_time
            speed = current / elapsed_time if elapsed_time > 0 else 0
            eta_seconds = (total - current) / speed if speed > 0 else 0

            bar = '█' * int(percent / 5) + '░' * (20 - int(percent / 5))
            status_text = (
                f"{status_text_prefix}"
                f"📊 Progress: {percent:.1f}% ({format_size(current)} / {format_size(total)})\n"
                f"🚀 Speed: {format_size(speed)}/s\n"
                f"⏱️ ETA: {format_time(eta_seconds)}\n"
                f"[{bar}]"
            )

            await client.edit_message(chat_id, msg_id, status_text)
            setattr(update_progress_message, message_key, now)  # Store last update time

    except FloodWaitError as e:
        logger.warning(f"Flood wait during progress update: {e.seconds}s. Skipping update.")
        # Store future time to prevent updates during flood wait
        setattr(update_progress_message, message_key, now + e.seconds)
    except MessageNotModifiedError:
        # If the message hasn't changed, update the timestamp anyway to prevent rapid retries
        setattr(update_progress_message, message_key, now)
    except Exception as e:
        logger.warning(f"Failed to update progress message {msg_id}: {e}")

async def safe_edit_message(client, chat_id, message_id, new_text):
    """
    Safely edit a message by comparing content first to avoid 'not modified' errors.
    DEPRECATED in favor of handling MessageNotModifiedError in update_progress_message.
    Kept for reference or other use cases if needed.
    """
    try:
        await client.edit_message(chat_id, message_id, new_text)
    except MessageNotModifiedError:
        logger.debug(f"Message {message_id} not modified.")
        pass  # Ignore if message is the same
    except FloodWaitError as e:
        logger.warning(f"Flood wait editing message {message_id}: {e.seconds}s")
        await asyncio.sleep(e.seconds)  # Wait before potentially retrying elsewhere
    except Exception as e:
        logger.error(f"Error editing message {message_id}: {e}")


# --- File Upload Helpers ---

async def upload_file_with_telethon(client, chat_id, file_path, caption=None, task=None, task_manager=None) -> Tuple[bool, Optional[int]]:
    """Upload a single file using Telethon with progress tracking and status messages."""
    status_msg = None
    status_msg_id = None
    success = False
    try:
        if not os.path.exists(file_path):
            logger.error(f"File not found for upload: {file_path}")
            return False, None

        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        # Send initial status message
        try:
            status_msg = await client.send_message(
                chat_id, f"📤 Preparing to upload: {file_name}\nSize: {format_size(file_size)}"
            )
            status_msg_id = status_msg.id
            if task and task_manager:
                # Link status message to task for potential cleanup
                task.add_temp_message(status_msg_id)
                task_manager.update_task(task.id, message_id=status_msg_id)  # Maybe update main task msg?
        except Exception as e:
            logger.error(f"Failed to send initial upload status message: {e}")
            # Proceed without status updates if sending fails

        upload_start_time = time.time()

        # Define the progress callback function
        async def progress_callback_async(current, total):
            if status_msg_id:  # Only update if we have a status message
                await update_progress_message(
                    client, chat_id, status_msg_id, current, total,
                    file_name, upload_start_time, status_text_prefix=f"📤 Uploading: {file_name}\n"
                )

        # Use the built-in Telethon upload with the async progress callback
        await client.send_file(
            chat_id,
            file=file_path,
            caption=caption or f"📁 {file_name}",
            progress_callback=progress_callback_async  # Pass the async function directly
        )

        success = True
        upload_duration = time.time() - upload_start_time

        # Delete the progress message AFTER successful upload
        if status_msg_id:
            try:
                await client.delete_messages(chat_id, status_msg_id)
                logger.debug(f"Deleted progress message {status_msg_id}")
                if task and status_msg_id in task.temp_message_ids:
                    task.temp_message_ids.remove(status_msg_id)
                    if task_manager:
                        task_manager._save_task(task)
            except Exception as e:
                logger.warning(f"Failed to delete progress message {status_msg_id}: {e}")

        logger.info(f"Successfully uploaded {file_name} ({format_size(file_size)}) in {format_time(upload_duration)}")
        return True, status_msg_id  # Return success and the ID of the status message

    except Exception as e:
        logger.error(f"Upload error for {file_path}: {e}", exc_info=True)
        error_msg = f"❌ Upload failed: {os.path.basename(file_path)}\nError: {str(e)[:100]}"  # Truncate long errors
        if status_msg_id:
            try:
                await client.edit_message(chat_id, status_msg_id, error_msg)
            except Exception:
                try:
                    await client.send_message(chat_id, error_msg)
                except Exception:
                    pass
        else:
            try:
                await client.send_message(chat_id, error_msg)
            except Exception:
                pass

        return False, status_msg_id  # Return failure and the status message ID (if any)


async def send_to_telegram(client, file_path: str, chat_id: int, task: Optional[Task] = None, task_manager=None):
    """Sends the file to Telegram, handling potential splitting and cleanup."""
    
    if not os.path.exists(file_path):
        logger.error(f"File not found for sending: {file_path}")
        if task and task_manager and task.message_id:
             try:
                 await client.edit_message(chat_id, task.message_id, f"❌ Task {task.id} Error: Final file not found.")
             except Exception: pass
        elif task: # Send new message if edit failed or no message_id
             try:
                 await client.send_message(chat_id, f"❌ Task {task.id} Error: Final file not found.")
             except Exception: pass
        # Update task status if it wasn't already failed
        if task and task.status == TaskStatus.COMPLETED: # Check if it was marked completed before upload
             task.update_status(TaskStatus.FAILED, "Final file not found before upload")
             if task_manager: task_manager._save_task(task)
        return

    # Sanitize file path to fix special character issues
    original_file_path = file_path
    if os.path.isfile(file_path):
        # Check if file contains problematic characters and rename if needed
        dir_path = os.path.dirname(file_path)
        filename = os.path.basename(file_path)
        # Replace problematic characters
        safe_filename = re.sub(r'[<>:"/\\|?*\[\]]', '_', filename)
        if safe_filename != filename:
            new_file_path = os.path.join(dir_path, safe_filename)
            try:
                os.rename(file_path, new_file_path)
                file_path = new_file_path
                logger.info(f"Renamed file to safe path: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to rename file to safe path: {e}")
    elif os.path.isdir(file_path):
        # Handle directory case - rename if needed
        dir_path = os.path.dirname(file_path)
        dirname = os.path.basename(file_path)
        safe_dirname = re.sub(r'[<>:"/\\|?*\[\]]', '_', dirname)
        if safe_dirname != dirname:
            new_dir_path = os.path.join(dir_path, safe_dirname)
            try:
                os.rename(file_path, new_dir_path)
                file_path = new_dir_path
                logger.info(f"Renamed directory to safe path: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to rename directory to safe path: {e}")

    if os.path.isfile(file_path):
        file_size = os.path.getsize(file_path)
        filename = os.path.basename(file_path)
        
        # Check if file is too large and needs compression
        if file_size > MAX_FILE_SIZE and HAS_COMPRESSOR:
            logger.info(f"File {filename} ({format_size(file_size)}) exceeds limit, compressing...")
            if task and task.message_id:
                try:
                    await client.edit_message(chat_id, task.message_id, 
                        f"📦 Task {task.id}: File too large, creating ZIP archive...\n"
                        f"File: {filename}\nSize: {format_size(file_size)}")
                except Exception: pass
            
            # Compress the file
            zip_name = f"{os.path.splitext(filename)[0]}.zip"
            part_paths, temp_dir = await stream_compress([file_path], zip_name, ZIP_PART_SIZE, chat_id, task, client, task_manager)
            
            if not part_paths:
                error_msg = temp_dir or "Compression failed - no parts created"
                logger.error(f"File compression failed: {error_msg}")
                if task and task.message_id:
                    try:
                        await client.edit_message(chat_id, task.message_id, f"❌ Task {task.id}: Compression failed: {error_msg}")
                        # Schedule error message for deletion after 60 seconds
                        from utils.message_tracker import message_tracker
                        await message_tracker.schedule_deletion(client, task, task.message_id, 60)
                    except Exception: pass
                return
            
            if part_paths:
                # Upload zip parts using improved handler for better stability
                from utils.improved_upload import upload_zip_parts_improved
                success = await upload_zip_parts_improved(client, chat_id, part_paths, task, task_manager)
                
                # Clean up temp directory after upload
                if temp_dir and os.path.exists(temp_dir):
                    try:
                        shutil.rmtree(temp_dir)
                        logger.info(f"Cleaned up compression temp directory: {temp_dir}")
                    except Exception as e:
                        logger.warning(f"Failed to clean up temp directory {temp_dir}: {e}")
                
                # Clean up progress message after successful upload
                if success and task and task.message_id:
                    try:
                        await client.delete_messages(chat_id, task.message_id)
                        if task.message_id in task.temp_message_ids:
                            task.temp_message_ids.remove(task.message_id)
                            if task_manager: task_manager._save_task(task)
                    except Exception as e:
                        logger.warning(f"Failed to delete progress message after zip upload: {e}")
                
                # Clean up original file
                try:
                    os.remove(file_path)
                    logger.info(f"Cleaned up original large file: {file_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up original file: {e}")
                
                return
    elif os.path.isdir(file_path):
        # Handle directory upload by compressing
        logger.info(f"Directory detected, compressing: {file_path}")
        if task and task.message_id:
            try:
                await client.edit_message(chat_id, task.message_id, 
                    f"📦 Task {task.id}: Compressing directory...\n"
                    f"Directory: {os.path.basename(file_path)}")
            except Exception: pass
        
        # Get all files in directory
        file_list = []
        for root, dirs, files in os.walk(file_path):
            for file in files:
                file_list.append(os.path.join(root, file))
        
        if not file_list:
            logger.error(f"No files found in directory: {file_path}")
            if task and task.message_id:
                try:
                    await client.edit_message(chat_id, task.message_id, f"❌ Task {task.id}: No files found in directory")
                except Exception: pass
            return
        
        # Compress directory
        # Remove any existing extension to prevent double extensions like .zip.zip
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        zip_name = f"{base_name}.zip"
        part_paths, temp_dir = await stream_compress(file_list, zip_name, ZIP_PART_SIZE, chat_id, task, client, task_manager)
        
        if not part_paths:
            error_msg = temp_dir or "Compression failed - no parts created"
            logger.error(f"Directory compression failed: {error_msg}")
            if task and task.message_id:
                try:
                    await client.edit_message(chat_id, task.message_id, f"❌ Task {task.id}: Compression failed: {error_msg}")
                    # Schedule error message for deletion after 60 seconds
                    from utils.message_tracker import message_tracker
                    await message_tracker.schedule_deletion(client, task, task.message_id, 60)
                except Exception: pass
            return
        
        if part_paths:
            # Upload zip parts with enhanced error handling
            try:
                from utils.improved_upload import upload_zip_parts_improved
                success = await upload_zip_parts_improved(client, chat_id, part_paths, task, task_manager)
                
                if not success:
                    # If upload failed, notify user
                    if task and task.message_id:
                        try:
                            await client.edit_message(
                                chat_id, 
                                task.message_id, 
                                f"❌ Task {task.id}: Upload failed\nSome ZIP parts could not be uploaded. Please check your connection and try again."
                            )
                        except Exception:
                            try:
                                await client.send_message(
                                    chat_id, 
                                    f"❌ Task {task.id}: Upload failed\nSome ZIP parts could not be uploaded."
                                )
                            except Exception:
                                pass
                
            except Exception as e:
                logger.error(f"Critical error during zip upload: {e}", exc_info=True)
                if task and task.message_id:
                    try:
                        await client.edit_message(
                            chat_id, 
                            task.message_id, 
                            f"❌ Task {task.id}: Critical upload error\n{str(e)[:100]}"
                        )
                    except Exception:
                        pass
            
            # Clean up temp directory after upload (always do this)
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Cleaned up compression temp directory: {temp_dir}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temp directory {temp_dir}: {e}")
            
            # Clean up progress message after successful upload
            if success and task and task.message_id:
                try:
                    await client.delete_messages(chat_id, task.message_id)
                    if task.message_id in task.temp_message_ids:
                        task.temp_message_ids.remove(task.message_id)
                        if task_manager: task_manager._save_task(task)
                except Exception as e:
                    logger.warning(f"Failed to delete progress message after directory zip upload: {e}")
            
            # Clean up original directory
            try:
                shutil.rmtree(file_path)
                logger.info(f"Cleaned up original directory: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up original directory: {e}")
            
            return

    # For files under size limit, proceed with normal upload
    file_size = os.path.getsize(file_path) if os.path.isfile(file_path) else 0
    filename = os.path.basename(file_path)
    
    logger.info(f"Uploading file: {filename}, size: {format_size(file_size)}, max_size: {format_size(MAX_FILE_SIZE)}")
    
    progress_msg_id = task.message_id if task else None    # Use a generic upload message if the specific one wasn't set or failed
    if progress_msg_id:
        try:
            await client.edit_message(chat_id, progress_msg_id, f"📤 Uploading: {filename} ({format_size(file_size)})...")
            # Make sure we track this message if we're using it for progress updates
            if task:
                task.add_temp_message(progress_msg_id)
                # Schedule for auto-deletion after upload completes
                from utils.message_tracker import message_tracker
                await message_tracker.schedule_deletion(client, task, progress_msg_id, 60)  # Auto-delete after 1 minute
        except Exception:
            logger.warning(f"Task {task.id if task else 'N/A'}: Failed to edit message to 'Uploading'.")
            progress_msg_id = None # Prevent trying to delete it later if edit failed

    # Initialize upload progress tracking
    last_upload_update_time = 0

    async def _upload_progress(current, total):
        nonlocal last_upload_update_time
        now = time.time()
        if now - last_upload_update_time > 3.0:  # Update every 3 seconds
            percent = round((current / total) * 100, 1)
            if progress_msg_id:
                try:
                    progress_bar = "█" * int(percent // 5) + "░" * (20 - int(percent // 5))
                    await client.edit_message(
                        chat_id, progress_msg_id,
                        f"📤 Uploading: {filename}\n"
                        f"[{progress_bar}] {percent}%\n"
                        f"📊 {format_size(current)} / {format_size(total)}"
                    )
                    last_upload_update_time = now
                except FloodWaitError as fwe:
                    logger.warning(f"Upload progress flood wait: {fwe.seconds}s")
                    last_upload_update_time = now + fwe.seconds  # Prevent updates during flood wait
                except Exception as e:
                    logger.warning(f"Failed to update upload progress: {e}")
            
    upload_progress_callback = _upload_progress

    try:
        logger.info(f"Task {task.id if task else 'N/A'}: Sending {file_path} ({format_size(file_size)}) to chat {chat_id}")
        
        # Verify file exists before attempting to send
        if not os.path.exists(file_path):
            error_msg = f"File not found: {file_path}"
            logger.error(error_msg)
            if progress_msg_id:
                try:
                    await client.edit_message(chat_id, progress_msg_id, f"❌ Error: {error_msg}")
                    # Schedule error message for deletion after 60 seconds
                    from utils.message_tracker import message_tracker
                    if task:
                        await message_tracker.schedule_deletion(client, task, progress_msg_id, 60)
                except Exception: pass
            return
        
        await client.send_file(
            chat_id,
            file_path,
            caption=f"`{filename}`",
            progress_callback=upload_progress_callback,
            supports_streaming=True
        )
        logger.info(f"Task {task.id if task else 'N/A'}: Successfully sent {filename} to chat {chat_id}")

        # Delete the progress message AFTER successful upload (but keep important messages)
        if progress_msg_id and task:
            try:
                # Only delete if this was a progress message, not an important status message
                if progress_msg_id in task.temp_message_ids:
                    await client.delete_messages(chat_id, progress_msg_id)
                    logger.debug(f"Task {task.id}: Deleted progress message {progress_msg_id}")
                    task.temp_message_ids.remove(progress_msg_id)
                    if task_manager: task_manager._save_task(task)
            except Exception as e:
                logger.warning(f"Task {task.id}: Failed to delete progress message {progress_msg_id}: {e}")
        elif progress_msg_id and not task:
            # If no task, it's safe to delete the progress message
            try:
                await client.delete_messages(chat_id, progress_msg_id)
                logger.debug(f"Deleted progress message {progress_msg_id} (no task)")
            except Exception as e:
                logger.warning(f"Failed to delete progress message {progress_msg_id}: {e}")

    except FloodWaitError as fwe:
         logger.error(f"Flood wait error during file upload: {fwe.seconds} seconds. Task {task.id if task else 'N/A'}")
         if progress_msg_id:
             try: await client.edit_message(chat_id, progress_msg_id, f"⏳ Upload delayed due to Telegram limits. Please wait...")
             except Exception: pass
         if task and task_manager:
             task.update_status(TaskStatus.FAILED, f"Upload failed due to flood wait ({fwe.seconds}s)")
             task_manager._save_task(task)

    except Exception as e:
        logger.error(f"Failed to send file {filename} for task {task.id if task else 'N/A'}: {e}", exc_info=True)
        if progress_msg_id:
            try:
                await client.edit_message(chat_id, progress_msg_id, f"❌ Upload Failed: An error occurred while sending '{filename}'.")
            except Exception: pass
        if task and task_manager:
            task.update_status(TaskStatus.FAILED, f"Upload failed: {str(e)}")
            task_manager._save_task(task)

    finally:
        if os.path.exists(file_path):
            try:
                if os.path.isdir(file_path):
                     shutil.rmtree(file_path)
                else:
                     os.remove(file_path)
                logger.info(f"Task {task.id if task else 'N/A'}: Cleaned up uploaded file: {file_path}")
            except Exception as e:
                logger.warning(f"Task {task.id if task else 'N/A'}: Failed to clean up file {file_path}: {e}")


async def upload_zip_parts(client, chat_id, part_paths, task=None, task_manager=None):
    """
    Uploads a list of zip part files to Telegram, one by one, handling progress and cleanup.
    Enhanced with better error handling, retries, and memory management for large files.
    Returns True if all parts uploaded successfully, False otherwise.
    """
    success = True
    upload_msg = None
    
    # Upload configuration for large files
    UPLOAD_RETRY_COUNT = 3
    UPLOAD_TIMEOUT = 300  # 5 minutes per part
    MEMORY_THRESHOLD = 100 * 1024 * 1024  # 100MB threshold for memory-intensive files
    
    # Send initial upload message for both single and multiple parts
    try:
        if len(part_paths) > 1:
            total_size = sum(size for _, size in part_paths)
            upload_msg = await client.send_message(
                chat_id,
                f"📤 Uploading {len(part_paths)} ZIP parts...\n"
                f"📦 Total size: {format_size(total_size)}\n"
                f"⚠️ Large file detected - upload may take time\n\n"
                f"⚡Powered by @ZakulikaCompressor_bot"
            )
        else:
            # Single ZIP file
            part_path, part_size = part_paths[0]
            part_name = os.path.basename(part_path)
            upload_msg = await client.send_message(
                chat_id,
                f"📤 Uploading ZIP file...\n"
                f"📁 {part_name}\n"
                f"📦 Size: {format_size(part_size)}\n"
                f"⚠️ Large file - please wait...\n\n"
                f"⚡Powered by @ZakulikaCompressor_bot"
            )
        
        if task:
            task.add_temp_message(upload_msg.id)
    except Exception as e:
        logger.warning(f"Failed to send initial upload message: {e}")
        upload_msg = None
    
    uploaded_parts = 0
    failed_parts = []
    
    for idx, (part_path, part_size) in enumerate(part_paths, start=1):
        if not os.path.exists(part_path):
            logger.error(f"ZIP part not found: {part_path}")
            failed_parts.append(f"Part {idx} (file not found)")
            success = False
            continue
            
        part_name = os.path.basename(part_path)
        caption = f"📦 Part {idx}/{len(part_paths)}: `{part_name}`\nSize: {format_size(part_size)}"
        
        # Determine if this is a memory-intensive upload
        is_large_file = part_size > MEMORY_THRESHOLD
        
        # Update upload progress for both single and multiple parts
        if upload_msg:
            try:
                if len(part_paths) > 1:
                    progress_text = (
                        f"📤 Uploading part {idx}/{len(part_paths)}...\n"
                        f"📁 {part_name}\n"
                        f"💾 Size: {format_size(part_size)}\n"
                        f"{'⚠️ Large part - may take time' if is_large_file else '🔄 Uploading...'}\n\n"
                        f"⚡Powered by @ZakulikaCompressor_bot"
                    )
                else:
                    progress_text = (
                        f"📤 Uploading ZIP file...\n"
                        f"📁 {part_name}\n"
                        f"💾 Size: {format_size(part_size)}\n"
                        f"{'⚠️ Large file - may take time' if is_large_file else '🔄 Uploading...'}\n\n"
                        f"⚡Powered by @ZakulikaCompressor_bot"
                    )
                await client.edit_message(chat_id, upload_msg.id, progress_text)
            except Exception:
                pass
        
        # Retry mechanism for each part
        part_uploaded = False
        retry_count = 0
        
        while retry_count < UPLOAD_RETRY_COUNT and not part_uploaded:
            try:
                # Add progress tracking for part uploads with reduced frequency for large files
                part_upload_start = time.time()
                last_progress_update = 0
                progress_update_interval = 5.0 if is_large_file else 2.0  # Less frequent updates for large files
                
                async def part_progress_callback(current, total):
                    nonlocal last_progress_update
                    now = time.time()
                    if now - last_progress_update > progress_update_interval:
                        try:
                            percent = round((current / total) * 100, 1)
                            progress_bar = "█" * int(percent // 5) + "░" * (20 - int(percent // 5))
                            
                            # Update progress for both single and multiple parts
                            if upload_msg:
                                try:
                                    elapsed = now - part_upload_start
                                    if total > current and current > 0:
                                        eta_seconds = (elapsed * (total - current)) / current
                                        eta_str = f" | ETA: {format_time(eta_seconds)}" if eta_seconds < 3600 else " | ETA: >1h"
                                    else:
                                        eta_str = ""
                                    
                                    if len(part_paths) > 1:
                                        progress_text = (
                                            f"📤 Uploading part {idx}/{len(part_paths)}...\n"
                                            f"📁 {part_name}\n"
                                            f"[{progress_bar}] {percent}%\n"
                                            f"📊 {format_size(current)} / {format_size(part_size)}{eta_str}\n"
                                            f"Attempt {retry_count + 1}/{UPLOAD_RETRY_COUNT}\n\n"
                                            f"⚡Powered by @ZakulikaCompressor_bot"
                                        )
                                    else:
                                        progress_text = (
                                            f"📤 Uploading ZIP file...\n"
                                            f"📁 {part_name}\n"
                                            f"[{progress_bar}] {percent}%\n"
                                            f"📊 {format_size(current)} / {format_size(part_size)}{eta_str}\n"
                                            f"Attempt {retry_count + 1}/{UPLOAD_RETRY_COUNT}\n\n"
                                            f"⚡Powered by @ZakulikaCompressor_bot"
                                        )
                                    await client.edit_message(chat_id, upload_msg.id, progress_text)
                                    last_progress_update = now
                                except Exception:
                                    pass  # Don't let progress updates break the upload
                        except Exception:
                            pass  # Ignore progress callback errors
                
                # Upload with timeout
                upload_task = asyncio.create_task(
                    client.send_file(
                        chat_id,
                        part_path,
                        caption=caption,
                        force_document=True,
                        progress_callback=part_progress_callback
                    )
                )
                
                # Wait for upload with timeout
                try:
                    await asyncio.wait_for(upload_task, timeout=UPLOAD_TIMEOUT)
                    part_uploaded = True
                    uploaded_parts += 1
                    logger.info(f"Successfully uploaded ZIP part {idx}/{len(part_paths)}: {part_name}")
                    
                    # Delete part after successful upload to save space
                    try:
                        os.remove(part_path)
                        logger.debug(f"Deleted uploaded ZIP part: {part_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete ZIP part {part_path}: {e}")
                        
                except asyncio.TimeoutError:
                    logger.warning(f"Upload timeout for part {part_name} (attempt {retry_count + 1})")
                    upload_task.cancel()
                    raise TimeoutError(f"Upload timeout after {UPLOAD_TIMEOUT} seconds")
                    
            except (ConnectionError, TimeoutError, OSError) as e:
                retry_count += 1
                logger.warning(f"Upload error for part {part_name} (attempt {retry_count}/{UPLOAD_RETRY_COUNT}): {e}")
                
                if retry_count < UPLOAD_RETRY_COUNT:
                    # Wait before retrying (exponential backoff)
                    wait_time = min(30, 5 * (2 ** (retry_count - 1)))  # Max 30 seconds
                    logger.info(f"Retrying part {part_name} in {wait_time} seconds...")
                    
                    if upload_msg:
                        try:
                            await client.edit_message(
                                chat_id, 
                                upload_msg.id, 
                                f"⚠️ Part {idx} failed - retrying in {wait_time}s...\n"
                                f"Attempt {retry_count}/{UPLOAD_RETRY_COUNT}\n"
                                f"Error: {str(e)[:100]}\n\n"
                                f"⚡Powered by @ZakulikaCompressor_bot"
                            )
                        except Exception:
                            pass
                    
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to upload part {part_name} after {UPLOAD_RETRY_COUNT} attempts")
                    failed_parts.append(f"Part {idx} ({str(e)[:50]})")
                    success = False
                    
            except FloodWaitError as fwe:
                logger.warning(f"Flood wait for part {part_name}: {fwe.seconds}s")
                if upload_msg:
                    try:
                        await client.edit_message(
                            chat_id, 
                            upload_msg.id, 
                            f"⏳ Rate limited - waiting {fwe.seconds}s...\n"
                            f"Part {idx}/{len(part_paths)}: {part_name}\n\n"
                            f"⚡Powered by @ZakulikaCompressor_bot"
                        )
                    except Exception:
                        pass
                
                await asyncio.sleep(fwe.seconds)
                retry_count += 1  # Count flood wait as a retry
                
            except Exception as e:
                logger.error(f"Unexpected error uploading part {part_name}: {e}", exc_info=True)
                failed_parts.append(f"Part {idx} (unexpected error)")
                success = False
                break
        
        # Force garbage collection after each part to free memory
        import gc
        gc.collect()
    
    # Clean up upload progress message and show final result
    if upload_msg:
        try:
            if success and uploaded_parts == len(part_paths):
                if len(part_paths) > 1:
                    success_text = (
                        f"✅ Upload complete!\n"
                        f"📦 {uploaded_parts}/{len(part_paths)} parts uploaded successfully\n"
                        f"💾 Total uploaded successfully\n\n"
                        f"⚡Powered by @ZakulikaCompressor_bot"
                    )
                else:
                    success_text = (
                        f"✅ ZIP upload complete!\n"
                        f"📁 File uploaded successfully\n"
                        f"💾 Upload completed without errors\n\n"
                        f"⚡Powered by @ZakulikaCompressor_bot"
                    )
                
                await client.edit_message(chat_id, upload_msg.id, success_text)
                await asyncio.sleep(5)  # Show success message longer
                await upload_msg.delete()
            else:
                # Show partial failure information
                failure_text = (
                    f"⚠️ Upload partially completed\n"
                    f"✅ Success: {uploaded_parts}/{len(part_paths)} parts\n"
                    f"❌ Failed: {len(failed_parts)} parts\n"
                )
                if failed_parts:
                    failure_text += f"Failed parts: {', '.join(failed_parts[:3])}"
                    if len(failed_parts) > 3:
                        failure_text += f" (+{len(failed_parts)-3} more)"
                failure_text += f"\n\n⚡Powered by @ZakulikaCompressor_bot"
                
                await client.edit_message(chat_id, upload_msg.id, failure_text)
        except Exception as e:
            logger.warning(f"Failed to update final upload message: {e}")
    
    # Clean up any remaining files on failure
    if not success:
        for part_path, _ in part_paths:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                    logger.debug(f"Cleaned up failed upload part: {part_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up part {part_path}: {e}")
    
    logger.info(f"Upload completed: {uploaded_parts}/{len(part_paths)} parts successful")
    return success


async def clean_user_directory(chat_id: int):
    """Clean up a user's entire download directory. Use with caution!"""
    user_dir = os.path.join(DOWNLOAD_DIR, str(chat_id))
    logger.warning(f"Attempting to clean entire directory: {user_dir}")

    if not os.path.exists(user_dir):
        logger.info(f"User directory {user_dir} does not exist, nothing to clean.")
        return

    try:
        shutil.rmtree(user_dir)
        logger.info(f"Successfully cleaned user directory: {user_dir}")
    except Exception as e:
        logger.error(f"Error cleaning user directory {user_dir}: {e}")


async def delete_temp_messages(client, task: Task, task_manager):
    """Deletes temporary messages associated with a task, typically on failure or cancellation."""
    if not task or not task.temp_message_ids:
        return

    logger.debug(f"Task {task.id}: Attempting to delete temporary messages: {task.temp_message_ids}")
    message_ids_to_delete = list(task.temp_message_ids)

    for msg_id in message_ids_to_delete:
        try:
            await client.delete_messages(task.chat_id, msg_id)
            logger.debug(f"Task {task.id}: Deleted temporary message {msg_id}")
            if msg_id in task.temp_message_ids:
                 task.temp_message_ids.remove(msg_id)
        except Exception as e:
            logger.warning(f"Task {task.id}: Failed to delete temporary message {msg_id}: {e}")
            if msg_id in task.temp_message_ids:
                 task.temp_message_ids.remove(msg_id)

    if len(message_ids_to_delete) > 0:
         task_manager._save_task(task)


async def send_tracked_message(client, chat_id: int, text: str, task: Optional[Task], parse_mode=None, buttons=None, auto_delete=None, **kwargs):
    """
    Sends a message and, if a task is provided, tracks its ID for later deletion.
    
    Args:
        client: The Telegram client
        chat_id: The chat ID to send the message to
        text: The message text
        task: The task to track the message with
        parse_mode: Telegram parse mode
        buttons: Message buttons
        auto_delete: If set, number of seconds after which the message should be automatically deleted
        **kwargs: Additional arguments for client.send_message
    """
    message = None
    try:
        message = await client.send_message(chat_id, text, parse_mode=parse_mode, buttons=buttons, **kwargs)
        if task and message:
            task.add_temp_message(message.id)
            
            # If auto_delete is set, schedule message for deletion
            if auto_delete is not None:
                from utils.message_tracker import message_tracker
                await message_tracker.schedule_deletion(client, task, message.id, auto_delete)
                logger.debug(f"Set message {message.id} to auto-delete after {auto_delete}s for task {task.id}")
    except Exception as e:
        logger.error(f"Failed to send/track message for task {task.id if task else 'N/A'} in chat {chat_id}: {e}")
    return message


async def delete_tracked_messages(client, task: Task, specific_ids: Optional[List[int]] = None):
    """
    Deletes messages tracked in a task.
    If specific_ids are provided, only those are deleted from chat and removed from task tracking.
    Otherwise, all messages currently in task.temp_message_ids are deleted and the list is cleared.
    
    This function is conservative and only deletes progress/temporary messages, not important status messages.
    """
    if not task:
        logger.warning("delete_tracked_messages called with no task.")
        return

    ids_to_delete_from_chat = []
    if specific_ids:
        # Filter out IDs not actually in the task's list to avoid errors if called with arbitrary IDs
        ids_to_delete_from_chat = [msg_id for msg_id in specific_ids if msg_id in task.temp_message_ids]
        # Remove only the specified (and existing) IDs from tracking
        for msg_id in ids_to_delete_from_chat:
            if msg_id in task.temp_message_ids: # Double check before removing
                task.temp_message_ids.remove(msg_id)
    else:
        # Only delete progress messages, keep important status messages
        ids_to_delete_from_chat = []
        ids_to_keep = []
        
        for msg_id in task.temp_message_ids:
            try:
                # Try to get the message to check its content
                message = await client.get_messages(task.chat_id, ids=msg_id)
                if message and message.text:
                    # Keep important messages (quality selection, final status, etc.)
                    if any(keyword in message.text.lower() for keyword in ['select', 'quality', 'completed', 'failed', 'error', 'success']):
                        ids_to_keep.append(msg_id)
                    else:
                        ids_to_delete_from_chat.append(msg_id)
                else:
                    # If we can't get the message, it might already be deleted
                    ids_to_delete_from_chat.append(msg_id)
            except Exception as e:
                logger.debug(f"Could not check message {msg_id}: {e}")
                # If we can't check, delete it to avoid orphaned tracking
                ids_to_delete_from_chat.append(msg_id)
        
        # Update the task's tracking to only keep important messages
        task.temp_message_ids = ids_to_keep

    if ids_to_delete_from_chat:
        try:
            await client.delete_messages(task.chat_id, ids_to_delete_from_chat)
            logger.info(f"Task {task.id}: Deleted {len(ids_to_delete_from_chat)} progress messages from chat {task.chat_id}")
        except Exception as e:
            # Log warning but don't re-add to task.temp_message_ids as they are meant to be deleted.
            logger.warning(f"Task {task.id}: Failed to delete some messages from chat {task.chat_id}: {e}")
