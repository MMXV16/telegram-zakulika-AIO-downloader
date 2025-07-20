import os
import time
import logging
import asyncio
import gc
import psutil
from typing import List, Tuple, Optional

from telethon.errors import FloodWaitError, TimeoutError, FilePartsInvalidError
# Note: ConnectionError and NetworkError are built-in Python exceptions, not from telethon
# Note: ConnectionError and NetworkError are built-in Python exceptions, not from telethon
from utils.formatting import format_size, format_time

logger = logging.getLogger(__name__)

async def upload_zip_parts_improved(client, chat_id, part_paths, task=None, task_manager=None):
    """
    Uploads a list of zip part files to Telegram, one by one, handling progress and cleanup.
    Enhanced with better error handling, retries, and memory management for large files.
    Returns True if all parts uploaded successfully, False otherwise.
    """
    success = True
    upload_msg = None
    
    # Enhanced upload configuration for very large files
    UPLOAD_RETRY_COUNT = 5  # Increased retries for large files
    BASE_UPLOAD_TIMEOUT = 600  # Base timeout: 10 minutes per part
    MEMORY_THRESHOLD = 50 * 1024 * 1024  # 50MB threshold for memory-intensive files
    CRITICAL_MEMORY_THRESHOLD = 85  # 85% memory usage triggers aggressive cleanup
    MAX_CHUNK_SIZE = 512 * 1024  # 512KB chunks for upload progress
    
    # Dynamic timeout calculation based on file size
    def calculate_timeout(file_size):
        """Calculate upload timeout based on file size - more time for larger files"""
        # Base timeout + 1 minute per 100MB
        size_factor = file_size / (100 * 1024 * 1024)  # 100MB chunks
        return min(BASE_UPLOAD_TIMEOUT + (size_factor * 60), 3600)  # Max 1 hour per part
    
    # Memory monitoring function
    def check_memory_status():
        """Check current memory usage and return status"""
        try:
            memory = psutil.virtual_memory()
            return {
                'percentage': memory.percent,
                'available': memory.available,
                'is_critical': memory.percent > CRITICAL_MEMORY_THRESHOLD
            }
        except Exception:
            return {'percentage': 0, 'available': 0, 'is_critical': False}
    
    # Force garbage collection and memory cleanup
    def force_memory_cleanup():
        """Aggressive memory cleanup"""
        try:
            gc.collect()  # Force garbage collection
            gc.collect()  # Run it twice for better results
            gc.collect()  # Third time for aggressive cleanup
        except Exception as e:
            logger.warning(f"Memory cleanup failed: {e}")
    
    # Calculate total size and check memory requirements
    total_size = sum(size for _, size in part_paths)
    logger.info(f"Starting upload of {len(part_paths)} parts, total size: {format_size(total_size)}")
    
    # Initial memory check
    memory_status = check_memory_status()
    if memory_status['is_critical']:
        logger.warning(f"Critical memory usage detected: {memory_status['percentage']:.1f}%")
        force_memory_cleanup()
    
    # Send initial upload message for both single and multiple parts
    try:
        if len(part_paths) > 1:
            upload_msg = await client.send_message(
                chat_id,
                f"📤 Uploading {len(part_paths)} ZIP parts...\n"
                f"📦 Total size: {format_size(total_size)}\n"
                f"⚠️ Large upload - this may take considerable time\n"
                f"🔄 Enhanced stability mode enabled\n\n"
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
                f"⚠️ Large file upload - optimized for stability\n"
                f"🔄 Enhanced retry logic enabled\n\n"
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
        upload_timeout = calculate_timeout(part_size)
        
        logger.info(f"Uploading part {idx}/{len(part_paths)}: {part_name} ({format_size(part_size)}) with {upload_timeout}s timeout")
        
        # Memory check before each part
        memory_status = check_memory_status()
        if memory_status['is_critical']:
            logger.warning(f"Critical memory before part {idx}: {memory_status['percentage']:.1f}%")
            force_memory_cleanup()
            # Wait a moment for cleanup to take effect
            await asyncio.sleep(2)
        
        # Update upload progress for both single and multiple parts
        if upload_msg:
            try:
                if len(part_paths) > 1:
                    progress_text = (
                        f"📤 Uploading part {idx}/{len(part_paths)}...\n"
                        f"📁 {part_name}\n"
                        f"💾 Size: {format_size(part_size)}\n"
                        f"⏱️ Timeout: {upload_timeout//60}min\n"
                        f"🧠 Memory: {memory_status['percentage']:.1f}%\n"
                        f"{'🔥 High memory usage detected' if memory_status['is_critical'] else '🔄 Preparing upload...'}\n\n"
                        f"⚡Powered by @ZakulikaCompressor_bot"
                    )
                else:
                    progress_text = (
                        f"📤 Uploading ZIP file...\n"
                        f"📁 {part_name}\n"
                        f"💾 Size: {format_size(part_size)}\n"
                        f"⏱️ Timeout: {upload_timeout//60}min\n"
                        f"🧠 Memory: {memory_status['percentage']:.1f}%\n"
                        f"{'🔥 High memory usage detected' if memory_status['is_critical'] else '🔄 Preparing upload...'}\n\n"
                        f"⚡Powered by @ZakulikaCompressor_bot"
                    )
                await client.edit_message(chat_id, upload_msg.id, progress_text)
            except Exception:
                pass
        
        # Enhanced retry mechanism for each part
        part_uploaded = False
        retry_count = 0
        
        while retry_count < UPLOAD_RETRY_COUNT and not part_uploaded:
            try:
                # Add progress tracking for part uploads with adaptive frequency
                part_upload_start = time.time()
                last_progress_update = 0
                progress_update_interval = 10.0 if is_large_file else 5.0  # Less frequent for large files
                
                async def part_progress_callback(current, total):
                    nonlocal last_progress_update
                    now = time.time()
                    if now - last_progress_update > progress_update_interval:
                        try:
                            percent = round((current / total) * 100, 1)
                            progress_bar = "█" * int(percent // 5) + "░" * (20 - int(percent // 5))
                            
                            # Check memory during upload
                            memory_check = check_memory_status()
                            
                            # Update progress for both single and multiple parts
                            if upload_msg:
                                try:
                                    elapsed = now - part_upload_start
                                    if total > current and current > 0:
                                        eta_seconds = (elapsed * (total - current)) / current
                                        eta_str = f" | ETA: {format_time(eta_seconds)}" if eta_seconds < 7200 else " | ETA: >2h"
                                    else:
                                        eta_str = ""
                                    
                                    if len(part_paths) > 1:
                                        progress_text = (
                                            f"📤 Uploading part {idx}/{len(part_paths)}...\n"
                                            f"📁 {part_name}\n"
                                            f"[{progress_bar}] {percent}%\n"
                                            f"📊 {format_size(current)} / {format_size(part_size)}{eta_str}\n"
                                            f"🔄 Attempt {retry_count + 1}/{UPLOAD_RETRY_COUNT}\n"
                                            f"🧠 Mem: {memory_check['percentage']:.1f}%\n\n"
                                            f"⚡Powered by @ZakulikaCompressor_bot"
                                        )
                                    else:
                                        progress_text = (
                                            f"📤 Uploading ZIP file...\n"
                                            f"📁 {part_name}\n"
                                            f"[{progress_bar}] {percent}%\n"
                                            f"📊 {format_size(current)} / {format_size(part_size)}{eta_str}\n"
                                            f"🔄 Attempt {retry_count + 1}/{UPLOAD_RETRY_COUNT}\n"
                                            f"🧠 Mem: {memory_check['percentage']:.1f}%\n\n"
                                            f"⚡Powered by @ZakulikaCompressor_bot"
                                        )
                                    await client.edit_message(chat_id, upload_msg.id, progress_text)
                                    last_progress_update = now
                                    
                                    # Emergency memory cleanup during upload
                                    if memory_check['is_critical']:
                                        logger.warning(f"Critical memory during upload: {memory_check['percentage']:.1f}%")
                                        gc.collect()
                                        
                                except Exception:
                                    pass  # Don't let progress updates break the upload
                        except Exception:
                            pass  # Ignore progress callback errors
                
                # Upload with adaptive timeout
                upload_task = asyncio.create_task(
                    client.send_file(
                        chat_id,
                        part_path,
                        caption=caption,
                        force_document=True,
                        progress_callback=part_progress_callback,
                        file_size=part_size  # Hint for better upload handling
                    )
                )
                
                # Wait for upload with enhanced timeout handling
                try:
                    await asyncio.wait_for(upload_task, timeout=upload_timeout)
                    part_uploaded = True
                    uploaded_parts += 1
                    logger.info(f"Successfully uploaded ZIP part {idx}/{len(part_paths)}: {part_name}")
                    
                    # Clean up immediately after successful upload to save disk space
                    try:
                        os.remove(part_path)
                        logger.debug(f"Deleted uploaded ZIP part: {part_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete ZIP part {part_path}: {e}")
                    
                    # Memory cleanup after each successful upload
                    force_memory_cleanup()
                        
                except asyncio.TimeoutError:
                    logger.warning(f"Upload timeout for part {part_name} after {upload_timeout}s (attempt {retry_count + 1})")
                    try:
                        upload_task.cancel()
                        await asyncio.sleep(1)  # Allow cancellation to complete
                    except Exception:
                        pass
                    raise TimeoutError(f"Upload timeout after {upload_timeout} seconds")
                    
            except (ConnectionError, OSError, TimeoutError) as e:
                retry_count += 1
                logger.warning(f"Upload error for part {part_name} (attempt {retry_count}/{UPLOAD_RETRY_COUNT}): {e}")
                
                # Force cleanup after each failed attempt
                force_memory_cleanup()
                
                if retry_count < UPLOAD_RETRY_COUNT:
                    # Exponential backoff with cap, but longer waits for large files
                    base_wait = 10 if is_large_file else 5
                    wait_time = min(120, base_wait * (2 ** (retry_count - 1)))  # Max 2 minutes
                    logger.info(f"Retrying part {part_name} in {wait_time} seconds...")
                    
                    if upload_msg:
                        try:
                            error_msg = str(e)[:80] + "..." if len(str(e)) > 80 else str(e)
                            await client.edit_message(
                                chat_id, 
                                upload_msg.id, 
                                f"⚠️ Part {idx} failed - retrying in {wait_time}s...\n"
                                f"🔄 Attempt {retry_count}/{UPLOAD_RETRY_COUNT}\n"
                                f"❌ Error: {error_msg}\n"
                                f"💾 Size: {format_size(part_size)}\n\n"
                                f"⚡Powered by @ZakulikaCompressor_bot"
                            )
                        except Exception:
                            pass
                    
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to upload part {part_name} after {UPLOAD_RETRY_COUNT} attempts")
                    failed_parts.append(f"Part {idx} ({str(e)[:30]}...)")
                    success = False
                    
            except FilePartsInvalidError as e:
                logger.error(f"File parts invalid error for {part_name} - file may be corrupted: {e}")
                logger.info(f"File info - path: {part_path}, size: {part_size}, exists: {os.path.exists(part_path)}")
                
                # Check if file is readable and has proper ZIP header
                try:
                    with open(part_path, 'rb') as check_file:
                        header = check_file.read(8)
                        logger.info(f"File header: {header.hex() if header else 'EMPTY'}")
                        if len(header) < 4 or not header.startswith(b'PK'):
                            logger.error(f"File {part_path} has invalid ZIP header - possible corruption")
                except Exception as check_error:
                    logger.error(f"Cannot read file {part_path}: {check_error}")
                
                logger.error(f"FilePartsInvalidError - skipping part {part_name}, cannot retry this error type")
                failed_parts.append(f"Part {idx} (corrupted file)")
                success = False
                break  # Don't retry FilePartsInvalidError - it indicates file corruption
                    
            except FloodWaitError as fwe:
                logger.warning(f"Flood wait for part {part_name}: {fwe.seconds}s")
                if upload_msg:
                    try:
                        await client.edit_message(
                            chat_id, 
                            upload_msg.id, 
                            f"⏳ Rate limited - waiting {fwe.seconds}s...\n"
                            f"📁 Part {idx}/{len(part_paths)}: {part_name}\n"
                            f"💾 Size: {format_size(part_size)}\n\n"
                            f"⚡Powered by @ZakulikaCompressor_bot"
                        )
                    except Exception:
                        pass
                
                await asyncio.sleep(fwe.seconds)
                # Don't count flood wait as a regular retry - it's not a failure
                
            except Exception as e:
                logger.error(f"Unexpected error uploading part {part_name}: {e}", exc_info=True)
                failed_parts.append(f"Part {idx} (unexpected error)")
                success = False
                break
        
        # Aggressive memory cleanup after each part (successful or failed)
        force_memory_cleanup()
        
        # Small delay between parts to allow system recovery
        if idx < len(part_paths):  # Don't wait after the last part
            await asyncio.sleep(1)
    
    # Final memory cleanup
    force_memory_cleanup()
    
    # Clean up upload progress message and show final result
    if upload_msg:
        try:
            if success and uploaded_parts == len(part_paths):
                if len(part_paths) > 1:
                    success_text = (
                        f"✅ Upload complete!\n"
                        f"📦 {uploaded_parts}/{len(part_paths)} parts uploaded successfully\n"
                        f"💾 Total size: {format_size(total_size)}\n"
                        f"🚀 All parts uploaded without errors\n\n"
                        f"⚡Powered by @ZakulikaCompressor_bot"
                    )
                else:
                    success_text = (
                        f"✅ ZIP upload complete!\n"
                        f"📁 File uploaded successfully\n"
                        f"💾 Size: {format_size(total_size)}\n"
                        f"🚀 Upload completed without errors\n\n"
                        f"⚡Powered by @ZakulikaCompressor_bot"
                    )
                
                await client.edit_message(chat_id, upload_msg.id, success_text)
                await asyncio.sleep(8)  # Show success message longer for large uploads
                try:
                    await upload_msg.delete()
                except Exception:
                    pass
            else:
                # Show detailed failure information
                failure_text = (
                    f"⚠️ Upload completed with issues\n"
                    f"✅ Success: {uploaded_parts}/{len(part_paths)} parts\n"
                    f"❌ Failed: {len(failed_parts)} parts\n"
                )
                if failed_parts:
                    failure_text += f"💥 Failed parts: {', '.join(failed_parts[:2])}"
                    if len(failed_parts) > 2:
                        failure_text += f" (+{len(failed_parts)-2} more)"
                failure_text += f"\n💾 Partial size uploaded: ~{format_size(total_size * uploaded_parts // len(part_paths))}"
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
    
    # Final status
    upload_status = "COMPLETE" if success else f"PARTIAL ({uploaded_parts}/{len(part_paths)})"
    logger.info(f"Upload {upload_status}: {uploaded_parts}/{len(part_paths)} parts successful, total size: {format_size(total_size)}")
    
    return success


async def handle_massive_file_upload(client, chat_id, file_path, task=None, task_manager=None, max_part_size=None):
    """
    Handle upload of extremely large files (>10GB) with smart splitting and robust error handling.
    This function is designed specifically for files that might crash the normal upload process.
    """
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return False, "File not found"
    
    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)
    
    # Use smaller part size for massive files to reduce memory pressure
    if max_part_size is None:
        if file_size > 20 * 1024**3:  # >20GB
            max_part_size = int(1.5 * 1024**3)  # 1.5GB parts
        elif file_size > 10 * 1024**3:  # >10GB
            max_part_size = int(1.7 * 1024**3)  # 1.7GB parts
        else:
            max_part_size = int(1.9 * 1024**3)  # 1.9GB parts (standard)
    
    logger.info(f"Handling massive file upload: {file_name} ({format_size(file_size)}) with {format_size(max_part_size)} parts")
    
    # Calculate number of parts needed
    num_parts = (file_size + max_part_size - 1) // max_part_size
    
    # Send initial message
    initial_msg = None
    try:
        initial_msg = await client.send_message(
            chat_id,
            f"🔨 Preparing massive file upload...\n"
            f"📁 {file_name}\n"
            f"💾 Size: {format_size(file_size)}\n"
            f"📦 Will split into {num_parts} parts ({format_size(max_part_size)} each)\n"
            f"⚠️ This may take a very long time\n\n"
            f"⚡Powered by @ZakulikaCompressor_bot"
        )
        if task:
            task.add_temp_message(initial_msg.id)
    except Exception as e:
        logger.warning(f"Failed to send initial massive upload message: {e}")
    
    # Split and upload the file in parts
    part_paths = []
    temp_dir = os.path.join(os.path.dirname(file_path), f"massive_split_{int(time.time())}")
    
    try:
        os.makedirs(temp_dir, exist_ok=True)
        
        # Split the file into parts
        if initial_msg:
            try:
                await client.edit_message(
                    chat_id,
                    initial_msg.id,
                    f"✂️ Splitting massive file...\n"
                    f"📁 {file_name}\n"
                    f"💾 Size: {format_size(file_size)}\n"
                    f"📦 Creating {num_parts} parts...\n\n"
                    f"⚡Powered by @ZakulikaCompressor_bot"
                )
            except Exception:
                pass
        
        # Split file into smaller parts
        with open(file_path, 'rb') as source_file:
            for part_num in range(1, num_parts + 1):
                part_name = f"{os.path.splitext(file_name)[0]}.part{part_num:03d}"
                part_path = os.path.join(temp_dir, part_name)
                
                # Memory-conscious file splitting
                bytes_to_read = min(max_part_size, file_size - source_file.tell())
                
                with open(part_path, 'wb') as part_file:
                    # Read and write in chunks to avoid memory issues
                    chunk_size = 8 * 1024 * 1024  # 8MB chunks
                    remaining = bytes_to_read
                    
                    while remaining > 0:
                        chunk = source_file.read(min(chunk_size, remaining))
                        if not chunk:
                            break
                        part_file.write(chunk)
                        remaining -= len(chunk)
                        
                        # Force garbage collection periodically
                        if remaining % (chunk_size * 10) == 0:
                            gc.collect()
                
                actual_part_size = os.path.getsize(part_path)
                part_paths.append((part_path, actual_part_size))
                logger.info(f"Created part {part_num}/{num_parts}: {part_name} ({format_size(actual_part_size)})")
                
                # Update splitting progress
                if initial_msg and part_num % 5 == 0:  # Update every 5 parts
                    try:
                        await client.edit_message(
                            chat_id,
                            initial_msg.id,
                            f"✂️ Splitting massive file...\n"
                            f"📁 {file_name}\n"
                            f"💾 Size: {format_size(file_size)}\n"
                            f"📦 Created {part_num}/{num_parts} parts...\n\n"
                            f"⚡Powered by @ZakulikaCompressor_bot"
                        )
                    except Exception:
                        pass
        
        # Delete the original file to save space
        try:
            os.remove(file_path)
            logger.info(f"Deleted original massive file: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to delete original file: {e}")
        
        # Update message for upload phase
        if initial_msg:
            try:
                await client.edit_message(
                    chat_id,
                    initial_msg.id,
                    f"🚀 Starting upload of massive file...\n"
                    f"📁 {file_name}\n"
                    f"💾 Size: {format_size(file_size)}\n"
                    f"📦 {len(part_paths)} parts ready\n\n"
                    f"⚡Powered by @ZakulikaCompressor_bot"
                )
                await asyncio.sleep(2)
                await initial_msg.delete()
            except Exception:
                pass
        
        # Upload the parts using the improved upload function
        success = await upload_zip_parts_improved(client, chat_id, part_paths, task, task_manager)
        
        return success, "Upload completed" if success else "Upload failed"
        
    except Exception as e:
        logger.error(f"Error during massive file upload: {e}", exc_info=True)
        
        # Cleanup on error
        if os.path.exists(temp_dir):
            try:
                import shutil
                shutil.rmtree(temp_dir)
                logger.info(f"Cleaned up temp directory: {temp_dir}")
            except Exception as cleanup_error:
                logger.error(f"Failed to cleanup temp directory: {cleanup_error}")
        
        if initial_msg:
            try:
                await client.edit_message(
                    chat_id,
                    initial_msg.id,
                    f"❌ Massive file upload failed\n"
                    f"📁 {file_name}\n"
                    f"💾 Size: {format_size(file_size)}\n"
                    f"❌ Error: {str(e)[:100]}\n\n"
                    f"⚡Powered by @ZakulikaCompressor_bot"
                )
            except Exception:
                pass
        
        return False, f"Massive upload failed: {e}"
    
    finally:
        # Final cleanup
        if os.path.exists(temp_dir):
            try:
                import shutil
                shutil.rmtree(temp_dir)
            except Exception:
                pass
