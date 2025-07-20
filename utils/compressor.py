import os
import time
import logging
import asyncio
import shutil
import gc
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

# Try to import zipstream package, fallback to zipfile if not available
try:
    import zipstream
    HAS_ZIPSTREAM = True
    
    # Try to determine which zipstream version we have
    if hasattr(zipstream, '__version__'):
        logger.info(f"zipstream version: {zipstream.__version__}")
    
    # Test zipstream initialization to detect API
    try:
        if hasattr(zipstream, 'ZipFile'):
            test_zip = zipstream.ZipFile()
            ZIPSTREAM_CLASS = 'ZipFile'
        elif hasattr(zipstream, 'ZipStream'):
            test_zip = zipstream.ZipStream()
            ZIPSTREAM_CLASS = 'ZipStream'
        else:
            raise ImportError("No ZipFile or ZipStream class found")
            
        test_zip = None  # Clean up test object
        ZIPSTREAM_INIT_METHOD = "default"
        logger.info(f"zipstream.{ZIPSTREAM_CLASS}() works with default constructor")
    except Exception as e:
        logger.warning(f"zipstream initialization failed: {e}")
        ZIPSTREAM_INIT_METHOD = "fallback"
        ZIPSTREAM_CLASS = None
        
except ImportError:
    import zipfile
    HAS_ZIPSTREAM = False
    ZIPSTREAM_INIT_METHOD = None
    ZIPSTREAM_CLASS = None
    zipfile = zipfile  # Ensure zipfile is available for fallback

if not HAS_ZIPSTREAM:
    logger.warning("zipstream not available, using standard zipfile (memory intensive for large files)")

# Always import zipfile for fallback
if 'zipfile' not in locals():
    import zipfile

# Assuming config and utils.formatting are in the parent directory or accessible
from config import TEMP_DIR
from utils.formatting import format_size

# Assuming bot_client.client and task_manager are initialized elsewhere and passed or imported
# from bot_client import client # Example import
# from task_manager import task_manager # Example import

logger = logging.getLogger(__name__)

async def stream_compress(file_paths: List[str], zip_name: str, max_part_size: int, chat_id: int, task=None, client=None, task_manager=None) -> Tuple[List[Tuple[str, int]], Optional[str]]:
    """Stream compress files to disk with progress tracking - robust implementation"""
    
    logger.info(f"Starting compression - max_part_size: {format_size(max_part_size)}")
    
    # For very large files or if zipstream has failed before, use fallback directly
    total_input_size = 0
    for file_path in file_paths:
        if os.path.isfile(file_path):
            total_input_size += os.path.getsize(file_path)
        elif os.path.isdir(file_path):
            for root, dirs, files in os.walk(file_path):
                for file in files:
                    full_path = os.path.join(root, file)
                    if os.path.exists(full_path):
                        total_input_size += os.path.getsize(full_path)
    
    # If total size is very large (>20GB) or zipstream is not available, use fallback directly
    if total_input_size > 20 * 1024**3 or not HAS_ZIPSTREAM:
        logger.info(f"Using fallback compression directly - input size: {format_size(total_input_size)}, zipstream available: {HAS_ZIPSTREAM}")
        return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
    
    if not HAS_ZIPSTREAM:
        # Fallback to simple zipfile compression
        return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
    
    # Validate inputs
    if not file_paths:
        logger.error("No file paths provided for compression")
        return [], "No files to compress"
        
    # Check if all files exist and flatten directory structures
    valid_files = []
    for file_path in file_paths:
        if os.path.isfile(file_path):
            valid_files.append(file_path)
            logger.debug(f"Added file: {file_path} ({format_size(os.path.getsize(file_path))})")
        elif os.path.isdir(file_path):
            # Walk through directory and add all files
            for root, dirs, files in os.walk(file_path):
                for file in files:
                    full_path = os.path.join(root, file)
                    valid_files.append(full_path)
                    logger.debug(f"Added directory file: {full_path} ({format_size(os.path.getsize(full_path))})")
        else:
            logger.warning(f"Path not found, skipping: {file_path}")
    
    if not valid_files:
        logger.error("No valid files found for compression")
        return [], "No valid files to compress"
    
    logger.info(f"Found {len(valid_files)} files to compress")
    
    # Calculate total size
    total_size = 0
    for file_path in valid_files:
        try:
            file_size = os.path.getsize(file_path)
            total_size += file_size
            logger.debug(f"File {file_path}: {format_size(file_size)}")
        except OSError as e:
            logger.warning(f"Could not get size for {file_path}: {e}")
    
    logger.info(f"Total size to compress: {format_size(total_size)}")
    logger.info(f"Max part size: {format_size(max_part_size)}")
    
    if total_size == 0:
        logger.error("Total size is 0 - no data to compress")
        return [], "No data to compress"
    
    part_number = 1
    processed_size = 0
    last_update_time = time.time()
    multipart_generated = False

    # Use temporary directory
    temp_dir = os.path.join(TEMP_DIR, f"compress_{chat_id}_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)
    
    part_paths = []  # List of (path, size) tuples
    
    # Better file description for progress messages
    file_list = [os.path.basename(p) for p in valid_files[:3]]
    file_str = ", ".join(file_list) if len(file_list) <= 3 else f"{len(valid_files)} files"
    progress_msg = None

    try:
        # Get or create progress message
        if task and task.message_id and client:
            try:
                progress_msg = await client.get_messages(chat_id, ids=task.message_id)
            except Exception as e:
                logger.warning(f"Failed to get progress message {task.message_id}: {e}")
                progress_msg = None

        if not progress_msg and client:
            try:
                progress_msg = await client.send_message(
                    chat_id,
                    f"🔄 Creating ZIP for {file_str}...\nInitializing compression...\n\n⚡Powered by @ZakulikaCompressor_bot"
                )
                if task and task_manager:
                    task_manager.update_task(task.id, message_id=progress_msg.id)
                    task.add_temp_message(progress_msg.id)
            except Exception as e:
                logger.error(f"Failed to send initial progress message: {e}")

        # Create zipstream generator using the pre-tested method
        try:
            logger.info(f"zipstream module available: {HAS_ZIPSTREAM}, class: {ZIPSTREAM_CLASS}, init method: {ZIPSTREAM_INIT_METHOD}")
            
            if ZIPSTREAM_INIT_METHOD == "default" and ZIPSTREAM_CLASS:
                # Use the method we know works
                zipstream_class = getattr(zipstream, ZIPSTREAM_CLASS)
                z = zipstream_class()
                logger.info(f"Using zipstream.{ZIPSTREAM_CLASS}() with default constructor")
            else:
                # Try different approaches for compatibility
                z = None
                
                # Try ZipStream first (more common in zipstream-ng)
                if hasattr(zipstream, 'ZipStream'):
                    for init_args in [
                        {},  # No arguments
                        {'allowZip64': True},  # With allowZip64
                        {'compress_type': 8},  # With compress_type (ZIP_DEFLATED = 8)
                    ]:
                        try:
                            z = zipstream.ZipStream(**init_args)
                            logger.info(f"Using zipstream.ZipStream with args: {init_args}")
                            break
                        except TypeError as te:
                            logger.debug(f"zipstream.ZipStream failed with {init_args}: {te}")
                            continue
                        except Exception as e:
                            logger.debug(f"zipstream.ZipStream unexpected error with {init_args}: {e}")
                            continue
                
                # Try ZipFile if ZipStream failed
                if z is None and hasattr(zipstream, 'ZipFile'):
                    for init_args in [
                        {},  # No arguments
                        {'allowZip64': True},  # With allowZip64
                        {'compress_type': 8},  # With compress_type (ZIP_DEFLATED = 8)
                    ]:
                        try:
                            z = zipstream.ZipFile(**init_args)
                            logger.info(f"Using zipstream.ZipFile with args: {init_args}")
                            break
                        except TypeError as te:
                            logger.debug(f"zipstream.ZipFile failed with {init_args}: {te}")
                            continue
                        except Exception as e:
                            logger.debug(f"zipstream.ZipFile unexpected error with {init_args}: {e}")
                            continue
                
                if z is None:
                    raise ImportError("Could not initialize any zipstream class")
                    
        except Exception as e:
            logger.warning(f"Failed to create zipstream object: {e}")
            logger.info("Falling back to standard zipfile compression")
            # Fall back to zipfile
            return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
        
        # Add all files to the zipstream
        files_added = 0
        for file_path in valid_files:
            if os.path.isfile(file_path):
                # Use relative path for archive name
                if len(file_paths) == 1 and os.path.isdir(file_paths[0]):
                    # If compressing a single directory, preserve structure
                    base_dir = file_paths[0]
                    arcname = os.path.relpath(file_path, os.path.dirname(base_dir))
                else:
                    # For multiple files or single files, use basename
                    arcname = os.path.basename(file_path)
                
                # Validate file before adding
                try:
                    file_size = os.path.getsize(file_path)
                    if file_size == 0:
                        logger.warning(f"Skipping empty file: {file_path}")
                        continue
                    
                    # Verify file is readable
                    with open(file_path, 'rb') as test_file:
                        test_file.read(1)  # Try to read first byte
                    
                    logger.debug(f"Adding file to zipstream: {file_path} -> {arcname} ({format_size(file_size)})")
                    
                except (OSError, IOError) as e:
                    logger.error(f"Cannot access file {file_path}: {e}")
                    continue
                
                # Try different methods to add files (zipstream-ng compatibility)
                try:
                    if hasattr(z, 'add'):
                        # zipstream-ng uses add() method
                        z.add(file_path, arcname=arcname)
                        logger.debug(f"Successfully added {arcname} to zipstream using add() - size: {format_size(file_size)}")
                        files_added += 1
                    elif hasattr(z, 'write'):
                        # Standard zipstream uses write() method
                        z.write(file_path, arcname=arcname)
                        logger.debug(f"Successfully added {arcname} to zipstream using write() - size: {format_size(file_size)}")
                        files_added += 1
                    elif hasattr(z, 'writestr'):
                        # Fallback: read file and use writestr (not recommended for large files)
                        logger.warning(f"Using writestr fallback for {file_path} - this may use excessive memory")
                        with open(file_path, 'rb') as f:
                            file_data = f.read()
                            z.writestr(arcname, file_data)
                            logger.debug(f"Successfully added {arcname} to zipstream using writestr() - size: {format_size(len(file_data))}")
                        files_added += 1
                    else:
                        raise AttributeError("No suitable method found to add files to zipstream")
                except Exception as e:
                    logger.error(f"Failed to add {file_path} to zipstream: {e}")
                    # If adding files fails, fall back to standard compression
                    logger.info("Falling back to standard zipfile compression due to zipstream error")
                    return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
        
        logger.info(f"Successfully added {files_added} files to zipstream")
        if files_added == 0:
            logger.error("No files were added to the zipstream")
            return [], "No files could be added to the ZIP archive"
        
        # Start writing to the first part file
        part_path = os.path.join(temp_dir, f"{zip_name}.zip.{part_number:03d}")
        current_size = 0
        
        # Add memory monitoring for large files
        import psutil
        initial_memory = psutil.virtual_memory().available
        memory_warning_threshold = 500 * 1024 * 1024  # 500MB minimum free memory
        
        # Open the first part file
        f = open(part_path, 'wb')
        
        try:
            chunk_count = 0
            total_chunks_written = 0
            logger.info("Starting to process zipstream chunks...")
            
            # Process zipstream chunks directly without validation to avoid iterator corruption
            # The validation was causing the iterator to be consumed and recreated incorrectly
            for chunk in z:
                chunk_len = len(chunk)
                
                # Validate chunk has content
                if chunk_len == 0:
                    logger.warning("Received empty chunk from zipstream")
                    continue
                
                # Check if we need to split to a new part BEFORE writing the chunk
                if current_size + chunk_len > max_part_size and current_size > 0:
                    # Flush and close current part properly
                    try:
                        f.flush()
                        os.fsync(f.fileno())  # Ensure data is written to disk
                    except (OSError, AttributeError):
                        pass
                    f.close()
                    multipart_generated = True
                    part_paths.append((part_path, current_size))
                    logger.info(f"Completed ZIP part {part_number}: {format_size(current_size)}")
                    
                    # Enhanced memory cleanup between parts for large files
                    if current_size > 1024**3:  # If part was > 1GB, do aggressive cleanup
                        gc.collect()
                        gc.collect()  # Double collection for large parts
                        logger.debug(f"Aggressive memory cleanup after large part {part_number}")
                    else:
                        gc.collect()  # Standard cleanup
                    
                    # Start new part
                    part_number += 1
                    part_path = os.path.join(temp_dir, f"{zip_name}.zip.{part_number:03d}")
                    current_size = 0
                    f = open(part_path, 'wb')
                
                # Special handling for very large chunks that might exceed max_part_size themselves
                if chunk_len > max_part_size:
                    logger.error(f"Single chunk too large ({format_size(chunk_len)}) for max_part_size ({format_size(max_part_size)})")
                    logger.info("Chunk exceeds max part size, falling back to standard zipfile compression")
                    # Close current file and clean up
                    if f and not f.closed:
                        f.close()
                    # Clean up any created parts
                    for p_path, _ in part_paths:
                        try:
                            if os.path.exists(p_path):
                                os.remove(p_path)
                        except Exception:
                            pass
                    try:
                        if os.path.exists(part_path):
                            os.remove(part_path)
                    except Exception:
                        pass
                    return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
                
                # Write the chunk to current part
                f.write(chunk)
                current_size += chunk_len
                processed_size += chunk_len
                chunk_count += 1
                total_chunks_written += chunk_len
                
                # Flush data to disk periodically for large files to prevent corruption
                if chunk_count % 100 == 0:  # Every 100 chunks
                    try:
                        f.flush()
                        os.fsync(f.fileno())  # Force write to disk
                    except (OSError, AttributeError):
                        pass  # Don't break compression if fsync fails
                
                # Log progress for first few chunks and periodically
                if chunk_count <= 10 or chunk_count % 1000 == 0:
                    logger.debug(f"Chunk {chunk_count}: {chunk_len} bytes, current_size: {format_size(current_size)}, total: {format_size(processed_size)}")
                
                # Monitor memory usage for large files
                if chunk_count % 100 == 0:  # Check every 100 chunks
                    try:
                        available_memory = psutil.virtual_memory().available
                        if available_memory < memory_warning_threshold:
                            logger.warning(f"Low memory detected: {format_size(available_memory)} available")
                            # Force garbage collection
                            gc.collect()
                    except Exception:
                        pass  # Don't let memory monitoring break compression
                
                # Update progress periodically
                now = time.time()
                if now - last_update_time > 2.0:  # Update every 2 seconds
                    last_update_time = now
                    if progress_msg and client:
                        try:
                            percentage = (processed_size / total_size) * 100 if total_size else 0
                            bar_length = 20
                            filled_length = int(bar_length * (processed_size / max(total_size, 1)))
                            bar = '█' * filled_length + '░' * (bar_length - filled_length)
                            
                            progress_text = (
                                f"🔄 Creating ZIP archive...\n"
                                f"[{bar}] {percentage:.1f}%\n"
                                f"Size: {format_size(processed_size)}/{format_size(total_size)}\n"
                                f"Part {part_number:03d} • Chunks: {chunk_count}\n\n⚡Powered by @ZakulikaCompressor_bot"
                            )
                            await client.edit_message(chat_id, progress_msg.id, progress_text)
                        except Exception as e:
                            if "Message not modified" not in str(e):
                                logger.warning(f"Failed to edit compression progress: {e}")
            
            logger.info(f"Finished processing {chunk_count} chunks, total data written: {format_size(total_chunks_written)}")
            
            # Validate that we actually wrote data
            if total_chunks_written == 0:
                logger.error("No data was written during zipstream processing - possible empty files or zipstream failure")
                logger.info("Falling back to standard zipfile compression")
                return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
            
            # Validate that the current part file has content
            if current_size == 0:
                logger.error("Final part file has no content")
                return [], "Compression produced no output"
        
        finally:
            # Always flush and close the file handle properly
            if f and not f.closed:
                try:
                    f.flush()
                    os.fsync(f.fileno())  # Ensure final data is written to disk
                except (OSError, AttributeError):
                    pass
                f.close()
        
        # Handle final part
        if multipart_generated:
            # Add the final part if it has content
            if current_size > 0:
                part_paths.append((part_path, current_size))
                logger.info(f"Completed final ZIP part {part_number}: {format_size(current_size)}")
        else:
            # Check if the single part exceeds max_part_size
            if current_size > max_part_size:
                logger.error(f"Single ZIP part too large: {format_size(current_size)} > {format_size(max_part_size)}")
                logger.info("Single part exceeds size limit, falling back to standard zipfile compression for proper splitting")
                # Clean up the oversized file
                try:
                    if os.path.exists(part_path):
                        os.remove(part_path)
                        logger.debug(f"Removed oversized single zipstream output: {part_path}")
                except Exception:
                    pass
                return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
            
            # If no splitting occurred and size is acceptable, rename to remove the .001 extension for single file
            # Ensure we don't create double .zip extensions
            if zip_name.endswith('.zip'):
                single_part_path = os.path.join(temp_dir, zip_name)
            else:
                single_part_path = os.path.join(temp_dir, f"{zip_name}.zip")
            try:
                if os.path.exists(part_path) and current_size > 0:
                    os.rename(part_path, single_part_path)
                    part_paths.append((single_part_path, current_size))
                    logger.info(f"Created single ZIP file: {format_size(current_size)}")
                else:
                    logger.error(f"No content written to ZIP file: {part_path}")
                    return [], "No content was written to ZIP file"
            except OSError as e:
                logger.error(f"Failed to rename single part: {e}")
                # If rename fails, use the original path if it has content
                if current_size > 0:
                    part_paths.append((part_path, current_size))
                else:
                    logger.error(f"Failed to create ZIP file and no content: {e}")
                    return [], f"Failed to create ZIP file: {e}"

        # Validate the created files to ensure they are not tiny/corrupt
        total_zip_size = sum(size for _, size in part_paths)
        expected_min_size = total_size * 0.01  # At least 1% of original size (very conservative)
        
        if total_zip_size < expected_min_size:
            logger.error(f"Zipstream compression produced suspiciously small output: {format_size(total_zip_size)} from {format_size(total_size)} input")
            logger.error(f"Expected minimum size: {format_size(expected_min_size)}, got: {format_size(total_zip_size)}")
            logger.info("Falling back to standard zipfile compression")
            # Clean up the bad files
            for part_path, _ in part_paths:
                try:
                    if os.path.exists(part_path):
                        os.remove(part_path)
                        logger.debug(f"Removed bad zipstream output: {part_path}")
                except Exception:
                    pass
            return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)

        # Critical validation: Check if any ZIP part exceeds Telegram's upload limits
        for part_path, part_size in part_paths:
            if part_size > max_part_size:
                logger.error(f"ZIP part {part_path} is too large: {format_size(part_size)} > {format_size(max_part_size)}")
                logger.info("Part exceeds max_part_size, falling back to standard zipfile compression for proper splitting")
                # Clean up the oversized files
                for p_path, _ in part_paths:
                    try:
                        if os.path.exists(p_path):
                            os.remove(p_path)
                            logger.debug(f"Removed oversized zipstream output: {p_path}")
                    except Exception:
                        pass
                return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)

        # Additional validation: ensure each part is reasonable size (not tiny files)
        for part_path, part_size in part_paths:
            if part_size < 1024:  # Less than 1KB is suspicious for real content
                logger.error(f"Detected tiny ZIP part: {part_path} ({format_size(part_size)})")
                logger.info("Falling back to standard zipfile compression due to tiny parts")
                # Clean up the bad files
                for p_path, _ in part_paths:
                    try:
                        if os.path.exists(p_path):
                            os.remove(p_path)
                            logger.debug(f"Removed tiny zipstream output: {p_path}")
                    except Exception:
                        pass
                return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
        
        # Additional validation: Check if ZIP files are valid/readable
        for part_path, part_size in part_paths:
            try:
                # Try to open and read the first few bytes to ensure file is not corrupted
                with open(part_path, 'rb') as test_file:
                    header = test_file.read(4)
                    # ZIP files should start with 'PK' signature (0x504b)
                    if not header.startswith(b'PK'):
                        logger.error(f"Invalid ZIP header in part: {part_path}")
                        logger.info("Falling back to standard zipfile compression due to invalid ZIP header")
                        # Clean up the bad files
                        for p_path, _ in part_paths:
                            try:
                                if os.path.exists(p_path):
                                    os.remove(p_path)
                                    logger.debug(f"Removed invalid zipstream output: {p_path}")
                            except Exception:
                                pass
                        return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
            except Exception as e:
                logger.error(f"Cannot validate ZIP part {part_path}: {e}")
                logger.info("Falling back to standard zipfile compression due to validation error")
                # Clean up the bad files
                for p_path, _ in part_paths:
                    try:
                        if os.path.exists(p_path):
                            os.remove(p_path)
                            logger.debug(f"Removed unreadable zipstream output: {p_path}")
                    except Exception:
                        pass
                return await _fallback_compress(file_paths, zip_name, max_part_size, chat_id, task, client, task_manager)
        
        logger.info(f"Zipstream compression validation passed - total size: {format_size(total_zip_size)} from {format_size(total_size)} input")

        # Final progress update
        if progress_msg and client:
            try:
                total_parts = len(part_paths)
                final_text = (
                    f"✅ ZIP creation complete!\n"
                    f"Parts: {total_parts}\n"
                    f"Total size: {format_size(processed_size)}\n\n⚡Powered by @ZakulikaCompressor_bot"
                )
                await client.edit_message(chat_id, progress_msg.id, final_text)
                
                # Schedule message deletion after 3 seconds
                await asyncio.sleep(3)
                try:
                    await progress_msg.delete()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Failed to send final compression message: {e}")

        gc.collect()
        return part_paths, temp_dir

    except Exception as e:
        logger.error(f"Error during stream compression: {e}", exc_info=True)
        
        # Cleanup on error
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as cleanup_error:
                logger.error(f"Error cleaning up temp directory {temp_dir}: {cleanup_error}")
        
        # Send error message if possible
        if progress_msg and client:
            try:
                await client.edit_message(chat_id, progress_msg.id, f"❌ Compression failed: {e}")
            except Exception:
                pass

        return [], f"Compression failed: {e}"
async def _fallback_compress(file_paths: List[str], zip_name: str, max_part_size: int, chat_id: int, task=None, client=None, task_manager=None) -> Tuple[List[Tuple[str, int]], Optional[str]]:
    """Fallback compression using standard zipfile when zipstream is not available"""
    logger.info(f"Using fallback compression with standard zipfile - max_part_size: {format_size(max_part_size)}")
    
    temp_dir = os.path.join(TEMP_DIR, f"compress_{chat_id}_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        # Flatten directory structures and collect all files
        all_files = []
        for file_path in file_paths:
            if os.path.isfile(file_path):
                all_files.append(file_path)
                logger.debug(f"Adding file for compression: {file_path} ({format_size(os.path.getsize(file_path))})")
            elif os.path.isdir(file_path):
                for root, dirs, files in os.walk(file_path):
                    for file in files:
                        full_path = os.path.join(root, file)
                        all_files.append(full_path)
                        logger.debug(f"Adding directory file for compression: {full_path}")
        
        if not all_files:
            logger.error("No files found to compress in fallback mode")
            return [], "No files found to compress"
        
        total_size = sum(os.path.getsize(path) for path in all_files if os.path.exists(path))
        
        # Send progress message
        progress_msg = None
        if task and task.message_id and client:
            try:
                progress_msg = await client.get_messages(chat_id, ids=task.message_id)
            except Exception:
                progress_msg = None
        
        if not progress_msg and client:
            try:
                file_list = [os.path.basename(p) for p in all_files[:3]]
                file_str = ", ".join(file_list) if len(file_list) <= 3 else f"{len(all_files)} files"
                progress_msg = await client.send_message(
                    chat_id,
                    f"🔄 Creating ZIP for {file_str}...\nUsing fallback compression...\n\n⚡Powered by @ZakulikaCompressor_bot"
                )
                if task and task_manager:
                    task_manager.update_task(task.id, message_id=progress_msg.id)
            except Exception as e:
                logger.error(f"Failed to send progress message: {e}")
        
        # Determine if we need multiple parts based on estimated compression
        estimated_compressed_size = total_size * 0.7  # Assume 70% compression ratio
        logger.info(f"Estimated compressed size: {format_size(estimated_compressed_size)}")
        
        if estimated_compressed_size <= max_part_size:
            # Single ZIP file
            zip_path = os.path.join(temp_dir, f"{zip_name}.zip")
            processed = 0
            last_update = time.time()
            
            logger.info(f"Creating single ZIP file: {zip_path}")
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zipf:
                for file_path in all_files:
                    if os.path.exists(file_path):
                        # Create appropriate archive name
                        if len(file_paths) == 1 and os.path.isdir(file_paths[0]):
                            # Preserve directory structure
                            base_dir = file_paths[0]
                            arcname = os.path.relpath(file_path, os.path.dirname(base_dir))
                        else:
                            arcname = os.path.basename(file_path)
                        
                        logger.debug(f"Adding {file_path} as {arcname}")
                        zipf.write(file_path, arcname)
                        
                        # Update progress
                        file_size = os.path.getsize(file_path)
                        processed += file_size
                        now = time.time()
                        if progress_msg and client and (now - last_update > 2.0):  # Every 2 seconds
                            try:
                                percent = (processed / total_size) * 100
                                bar_length = 20
                                filled_length = int(bar_length * (processed / max(total_size, 1)))
                                bar = '█' * filled_length + '░' * (bar_length - filled_length)
                                
                                await client.edit_message(
                                    chat_id,
                                    progress_msg.id,
                                    f"🔄 Compressing... (fallback mode)\n"
                                    f"[{bar}] {percent:.1f}%\n"
                                    f"Processed: {format_size(processed)}/{format_size(total_size)}\n\n"
                                    f"⚡Powered by @ZakulikaCompressor_bot"
                                )
                                last_update = now
                            except Exception as e:
                                if "Message not modified" not in str(e):
                                    logger.warning(f"Failed to update fallback progress: {e}")
            
            if not os.path.exists(zip_path):
                logger.error(f"ZIP file was not created: {zip_path}")
                return [], "ZIP file creation failed"
            
            zip_size = os.path.getsize(zip_path)
            logger.info(f"Created ZIP file with size: {format_size(zip_size)}")
            
            # Validate the ZIP file has reasonable content
            if zip_size < 50:  # A valid ZIP file should be at least 50 bytes
                logger.error(f"ZIP file is too small ({format_size(zip_size)}) - likely corrupted or empty")
                return [], "ZIP file appears to be empty or corrupted"
            
            # Try to validate the ZIP file by opening it
            try:
                with zipfile.ZipFile(zip_path, 'r') as test_zip:
                    file_list = test_zip.namelist()
                    if not file_list:
                        logger.error("ZIP file contains no files")
                        return [], "ZIP file is empty"
                    logger.info(f"ZIP file validation successful - contains {len(file_list)} files")
            except zipfile.BadZipFile as e:
                logger.error(f"ZIP file is corrupted: {e}")
                return [], f"ZIP file is corrupted: {e}"
            except Exception as e:
                logger.warning(f"Could not validate ZIP file: {e}, proceeding anyway")
            
            # Check if the final ZIP is actually too large and needs splitting
            if zip_size > max_part_size:
                logger.warning(f"Created ZIP ({format_size(zip_size)}) exceeds max part size ({format_size(max_part_size)}), splitting into parts")
                logger.info(f"Will create parts with naming: {zip_name}.zip.001, {zip_name}.zip.002, etc.")
                
                # Split the large ZIP file into multiple parts
                part_paths_split = await _split_large_zip_file(zip_path, zip_name, max_part_size, temp_dir, progress_msg, client, chat_id)
                
                if part_paths_split:
                    # Remove the original large ZIP file
                    try:
                        os.remove(zip_path)
                        logger.info(f"Removed original large ZIP file: {zip_path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove original large ZIP: {e}")
                    
                    # Update progress message
                    if progress_msg and client:
                        try:
                            await client.edit_message(
                                chat_id, 
                                progress_msg.id, 
                                f"✅ Compression complete!\n💾 Split into {len(part_paths_split)} parts\n\n⚡Powered by @ZakulikaCompressor_bot"
                            )
                            await asyncio.sleep(3)
                            await progress_msg.delete()
                        except Exception:
                            pass
                    
                    return part_paths_split, temp_dir
                else:
                    logger.error("Failed to split large ZIP file")
                    return [], "Failed to split large ZIP file"
            
            # Update progress message
            if progress_msg and client:
                try:
                    await client.edit_message(
                        chat_id, 
                        progress_msg.id, 
                        f"✅ Compression complete!\n💾 Size: {format_size(zip_size)}\n\n⚡Powered by @ZakulikaCompressor_bot"
                    )
                    await asyncio.sleep(3)
                    await progress_msg.delete()
                except Exception:
                    pass
            
            return [(zip_path, zip_size)], temp_dir
        else:
            # Large file set detected - create single large file with allowZip64
            logger.info("Large file set detected with zipfile fallback - creating single large ZIP")
            
            zip_path = os.path.join(temp_dir, f"{zip_name}.zip")
            processed = 0
            last_update = time.time()
            
            logger.info(f"Creating large ZIP file: {zip_path}")
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zipf:
                for file_path in all_files:
                    if os.path.exists(file_path):
                        if len(file_paths) == 1 and os.path.isdir(file_paths[0]):
                            base_dir = file_paths[0]
                            arcname = os.path.relpath(file_path, os.path.dirname(base_dir))
                        else:
                            arcname = os.path.basename(file_path)
                        
                        logger.debug(f"Adding large file {file_path} as {arcname}")
                        zipf.write(file_path, arcname)
                        
                        file_size = os.path.getsize(file_path)
                        processed += file_size
                        
                        # Update progress every 2 seconds
                        now = time.time()
                        if progress_msg and client and (now - last_update > 2.0):
                            try:
                                percent = (processed / total_size) * 100
                                bar_length = 20
                                filled_length = int(bar_length * (processed / max(total_size, 1)))
                                bar = '█' * filled_length + '░' * (bar_length - filled_length)
                                
                                await client.edit_message(
                                    chat_id,
                                    progress_msg.id,
                                    f"🔄 Compressing... (large dataset)\n"
                                    f"[{bar}] {percent:.1f}%\n"
                                    f"Processed: {format_size(processed)}/{format_size(total_size)}\n\n"
                                    f"⚡Powered by @ZakulikaCompressor_bot"
                                )
                                last_update = now
                            except Exception as e:
                                if "Message not modified" not in str(e):
                                    logger.warning(f"Failed to update large file progress: {e}")
            
            if not os.path.exists(zip_path):
                logger.error(f"Large ZIP file was not created: {zip_path}")
                return [], "Large ZIP file creation failed"
            
            zip_size = os.path.getsize(zip_path)
            logger.info(f"Created large ZIP file with size: {format_size(zip_size)}")
            
            # Critical validation: Check if the large ZIP exceeds max_part_size
            if zip_size > max_part_size:
                logger.warning(f"Created large ZIP ({format_size(zip_size)}) exceeds max part size ({format_size(max_part_size)}), splitting into parts")
                logger.info(f"Will create parts with naming: {zip_name}.zip.001, {zip_name}.zip.002, etc.")
                
                # Split the large ZIP file into multiple parts
                part_paths_split = await _split_large_zip_file(zip_path, zip_name, max_part_size, temp_dir, progress_msg, client, chat_id)
                
                if part_paths_split:
                    # Remove the original large ZIP file
                    try:
                        os.remove(zip_path)
                        logger.info(f"Removed original large ZIP file: {zip_path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove original large ZIP: {e}")
                    
                    # Update progress message
                    if progress_msg and client:
                        try:
                            await client.edit_message(
                                chat_id, 
                                progress_msg.id, 
                                f"✅ Compression complete!\n💾 Split into {len(part_paths_split)} parts\n\n⚡Powered by @ZakulikaCompressor_bot"
                            )
                            await asyncio.sleep(3)
                            await progress_msg.delete()
                        except Exception:
                            pass
                    
                    return part_paths_split, temp_dir
                else:
                    logger.error("Failed to split large ZIP file")
                    return [], "Failed to split large ZIP file"
            
            # If size is acceptable, return as single file
            if progress_msg and client:
                try:
                    await client.edit_message(
                        chat_id,
                        progress_msg.id,
                        f"✅ Compression complete!\n💾 Size: {format_size(zip_size)}\n\n⚡Powered by @ZakulikaCompressor_bot"
                    )
                    await asyncio.sleep(3)
                    await progress_msg.delete()
                except Exception:
                    pass
            
            return [(zip_path, zip_size)], temp_dir
        
    except Exception as e:
        logger.error(f"Error during fallback compression: {e}")
        # Cleanup
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
        return [], f"Fallback compression failed: {e}"

async def _split_large_zip_file(zip_path: str, zip_name: str, max_part_size: int, temp_dir: str, progress_msg=None, client=None, chat_id=None) -> List[Tuple[str, int]]:
    """Split a large ZIP file into multiple parts for upload"""
    try:
        zip_size = os.path.getsize(zip_path)
        logger.info(f"Splitting large ZIP file {zip_path} ({format_size(zip_size)}) into parts of {format_size(max_part_size)}")
        
        # Calculate number of parts needed
        num_parts = (zip_size + max_part_size - 1) // max_part_size
        logger.info(f"Will create {num_parts} parts")
        
        part_paths = []
        
        with open(zip_path, 'rb') as source_file:
            for part_num in range(1, num_parts + 1):
                part_name = f"{zip_name}.zip.{part_num:03d}"
                part_path = os.path.join(temp_dir, part_name)
                
                # Calculate bytes to read for this part
                bytes_to_read = min(max_part_size, zip_size - source_file.tell())
                
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
                
                actual_part_size = os.path.getsize(part_path)
                part_paths.append((part_path, actual_part_size))
                logger.info(f"Created ZIP part {part_num}/{num_parts}: {part_name} ({format_size(actual_part_size)})")
                
                # Update progress
                if progress_msg and client and part_num % 3 == 0:  # Update every 3 parts
                    try:
                        await client.edit_message(
                            chat_id,
                            progress_msg.id,
                            f"🔄 Splitting large ZIP...\n"
                            f"📦 Created {part_num}/{num_parts} parts\n"
                            f"💾 Total: {format_size(zip_size)}\n\n"
                            f"⚡Powered by @ZakulikaCompressor_bot"
                        )
                    except Exception:
                        pass
        
        logger.info(f"Successfully split ZIP into {len(part_paths)} parts")
        return part_paths
        
    except Exception as e:
        logger.error(f"Error splitting large ZIP file: {e}")
        return []
