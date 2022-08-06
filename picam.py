#!/usr/bin/env python3

import sys

if sys.stdout.isatty():
    print("Loading PiCam...")

from PIL import Image
from email import encoders
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import SysLogHandler
from pathlib import Path
from spidev import SpiDev
import RPi.GPIO as GPIO
import cv2
import logging
import logging.handlers
import math
import numpy as np
import os
import picamera
import picamera.array
import psutil
import signal
import smtplib
import ssl
import subprocess
import tempfile
import time

from config import *


""" Logging Setup """
logger = logging.getLogger("picam")
logger.setLevel(logging.DEBUG)
logger.propagate = False

if sys.stdout.isatty():
    logging.getLogger("PIL").setLevel(logging.CRITICAL+1)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler = logging.StreamHandler(sys.stderr)
else:
    formatter = logging.Formatter("[%(name)s] %(levelname)s %(message)s")
    handler = logging.handlers.SysLogHandler(address="/dev/log")
    handler.setLevel(logging.INFO)

handler.setFormatter(formatter)
logger.addHandler(handler)


""" GPIO Setup """
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(IR_LED_PIN, GPIO.OUT)
GPIO.setup(MOTION_SENSOR_PIN, GPIO.IN)
GPIO.setup(MODEM_POWER_PIN, GPIO.OUT)

last_motion_time = time.time()
motion_count = 0
false_alarm_count = 0


# https://picamera.readthedocs.io/en/release-1.13/api_array.html#pimotionanalysis
class DetectMotion(picamera.array.PiMotionAnalysis):
    def analyze(self, a):
        global last_motion_time
        global motion_count

        a = np.sqrt(np.square(a["x"].astype(np.float)) + np.square(a["y"].astype(np.float))).clip(1, 255).astype(np.uint8)

        if (a > MOTION_MAGNITUDE_MIN).sum() > MOTION_VECTORS_MIN:
            last_motion_time = time.time()
            motion_count += 1
            logger.debug(f"Motion detected! Motion count: {motion_count}")


class MCP3008:
    def __init__(self, bus = 0, device = 0):
        self.bus, self.device = bus, device
        self.spi = SpiDev()
        self.open()
        self.spi.max_speed_hz = 1000000

    def open(self):
        self.spi.open(self.bus, self.device)
        self.spi.max_speed_hz = 1000000

    def read(self, channel = 0):
        cmd1 = 4 | 2 | (( channel & 4) >> 2)
        cmd2 = (channel & 3) << 6

        adc = self.spi.xfer2([cmd1, cmd2, 0])
        data = ((adc[1] & 15) << 8) + adc[2]
        return data

    def close(self):
        self.spi.close()


class PPP:
    def __init__(self):
        self.proc = None
        self.connected = False

    def connect(self):
        start_time = time.time()
        self.proc = subprocess.Popen(PPP_CALL_COMMAND.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        while (time.time() - start_time) < PPP_TIMEOUT:
            with open("/proc/net/dev") as fp:
                for line in fp:
                    if line.split()[0].startswith("ppp0") and int(line.split()[2]) > 0:
                        self.connected = True

            if self.connected:
                break

    def disconnect(self):
        try:
            self.proc.kill()
        except Exception as e:
            pass

        try:
            os.remove("/var/lock/LCK..serial0")
        except Exception as e:
            pass

def graceful_exit(signum=None, frame=None):
    logger.info("Exiting...")

    try:
        toggle_modem_power(0)
    except:
        pass
    try:
        GPIO.output(IR_LED_PIN, GPIO.LOW)
    except:
        pass
    try:
        camera.close()
    except:
        pass
    try:
        GPIO.cleanup()
    except:
        pass

    sys.exit(0)


def get_battery_voltage():
    adc = MCP3008()

    v = []

    for i in range(10):
        v.append(adc.read(0) / 1023 * 1.165)
        time.sleep(0.1)

    adc.close()

    v_cur = sum(v) / len(v)
    v_per = float(((v_cur - VOLTAGE_MIN) / (VOLTAGE_MAX - VOLTAGE_MIN)) * 100)

    if v_per > 100:
        v_per = 100.0
    if v_per < 0:
        v_per = 0.0

    return v_cur, v_per


def get_cpu_temp():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return float(int(f.readline()) / 1000)


def get_disk_usage():
    return psutil.disk_usage(DATA_DIR).percent


def log_stats():
    v_cur, v_per = get_battery_voltage()
    return f"CPU Temp: {get_cpu_temp():.2f}°C, Battery: {v_cur:.2f}V ({v_per:.2f}%), Disk: {get_disk_usage():.2f}%"
    

def toggle_modem_power(status):
    GPIO.output(MODEM_POWER_PIN, not bool(status))


def generate_thumbnails(video_output_path, thumbnails_temp_path):
    try:
        cap = cv2.VideoCapture(video_output_path)

        frames_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frames_per_col = int(math.sqrt(VIDEO_THUMBNAILS_NUM))
        frames_per_thumbnail = int(frames_total/VIDEO_THUMBNAILS_NUM)

        w, h = VIDEO_RESOLUTION
        thumbnails_image = Image.new("RGBA", (w*frames_per_col, h*frames_per_col), 255)

        cur, row, col = 0, 0, 0

        for i in range(VIDEO_THUMBNAILS_NUM):
            cap.set(1, cur)
            ret, frame = cap.read()

            thumbnails_image.paste(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), (w*col, h*row))

            cur += frames_per_thumbnail

            if (i+1) % frames_per_col == 0:
                row += 1
            if (col+1) % frames_per_col == 0:
                col = 0
            else:
                col += 1

        cap.release()

        thumbnails_image.convert("RGB").resize(VIDEO_PREVIEW_RESOLUTION, Image.ANTIALIAS).save(thumbnails_temp_path.name, format="JPEG", quality=THUMBNAILS_QUALITY)
    except Exception as e:
        logger.error(e)
        return False

    return True


def send_mail(thumbnails_temp_path, thumbnails_filename):
    mail_sent = False
    smtp_connected = False
    smtp_ready = False

    message = MIMEMultipart()
    message["From"] = Header(SMTP_FROM)
    message["To"] = Header(SMTP_RCPT)
    message["Subject"] = Header("PiCam Triggered!")
    message.attach(MIMEText(f"{log_stats()}\n\n", "plain", "utf-8"))

    try:
        attachment = MIMEApplication(thumbnails_temp_path.read(), _subtype="jpg")
        attachment.add_header("Content-Disposition", "attachment", filename=thumbnails_filename)

        message.attach(attachment)
    except Exception as e:
        logger.error(e)

    try:
        server = smtplib.SMTP(host=SMTP_SERVER, port=SMTP_PORT, timeout=SMTP_TIMEOUT)
        # server.set_debuglevel(1)
        smtp_connected = True
    except Exception as e:
        logger.error(e)

    if smtp_connected:
        if SMTP_STARTTLS:
            try:
                server.starttls(context=ssl.create_default_context())
                smtp_ready = True
            except Exception as e:
                logger.debug(e)

        if smtp_ready:
            try:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            except Exception as e:
                logger.debug(e)

            try:
                server.sendmail(SMTP_FROM, SMTP_RCPT, message.as_string())
                mail_sent = True
            except Exception as e:
                logger.error(e)

        try:
            server.quit()
        except:
            pass

    return mail_sent


def motion_trigger_action(channel, force=False):
    trigger_start_time = time.time()
    filename_timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime(trigger_start_time))

    global last_motion_time
    global false_alarm_count
    global motion_count

    if GPIO.input(channel) == 0 and force is False:
        return

    logger.info("Motion Sensor triggered!")

    GPIO.output(IR_LED_PIN, GPIO.HIGH)

    logger.debug("Capturing Photo...")

    try:
        photo_output_path = os.path.join(DATA_DIR, f"{filename_timestamp}.jpg")

        with picamera.PiCamera() as camera:
            camera.rotation = CAMERA_ROTATION
            camera.color_effects = (128, 128)
            camera.resolution = PHOTO_RESOLUTION
            camera.annotate_background = picamera.Color("black")
            camera.annotate_foreground = picamera.Color("white")
            camera.annotate_text_size = 40
            camera.annotate_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            camera.capture(photo_output_path)
    except Exception as e:
        logger.error(f"Something went wrong :( ({e})")

    logger.debug("Capturing Video...")

    try:
        video_output_path = os.path.join(DATA_DIR, f"{filename_timestamp}.mp4")
        capture_start_timee = time.time()
        last_motion_time = capture_start_timee
        motion_count = 1
        false_alarm = False

        with tempfile.NamedTemporaryFile() as video_temp_path:
            with picamera.PiCamera() as camera:
                with DetectMotion(camera) as output:
                    camera.rotation = CAMERA_ROTATION
                    camera.color_effects = (128, 128)
                    camera.resolution = VIDEO_RESOLUTION
                    camera.framerate = VIDEO_FRAMERATE
                    camera.start_recording(video_temp_path, format="h264", motion_output=output)

                    camera.annotate_background = picamera.Color("black")
                    camera.annotate_foreground = picamera.Color("white")
                    camera.annotate_text_size = 16

                    while int(time.time() - capture_start_timee) < VIDEO_MAX_LENGTH:
                        if (time.time() - capture_start_timee) >= MOTION_THRESHOLD_TIME and motion_count < MOTION_THRESHOLD_COUNT:
                            false_alarm = True
                            break

                        camera.annotate_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                        camera.wait_recording(1)
                    
                    camera.stop_recording()

            GPIO.output(IR_LED_PIN, GPIO.LOW)

            if force is False and false_alarm is True:
                logger.error("False Alarm!")
                false_alarm_count += 1

                if false_alarm_count == THROTTLE_THRESHOLD:
                    logger.warning(f"Too many false alarms ({THROTTLE_THRESHOLD})! Throttling for {THROTTLE_DELAY} seconds")

                    time.sleep(THROTTLE_DELAY)
                    false_alarm_count = 0

                return

            false_alarm_count = 0

            logger.debug(f"Converting Video...")

            try:
                subprocess.check_output([FFMPEG_PATH, "-i", video_temp_path.name, "-c", "copy", video_output_path], stderr=subprocess.DEVNULL)
            except:
                logger.error("Video Error!")
                return

        logger.debug(f"Generating Thumbnails...")

        with tempfile.NamedTemporaryFile() as thumbnails_temp_path:
            if generate_thumbnails(video_output_path, thumbnails_temp_path):
                logger.debug("Connecting PPP...")

                toggle_modem_power(1)

                ppp = PPP()
                ppp.connect()
            
                if ppp.connected:
                    logger.debug("Sending Preview...")

                    if send_mail(thumbnails_temp_path, f"{Path(video_output_path).stem}.jpg") is False:
                        logger.error("SMTP Error! Mail not sent!")
                    else:
                        logger.debug("Mail sent successfully!")
                else:
                    logger.error("PPP Connection Error!")

                ppp.disconnect()

                toggle_modem_power(0)
    except Exception as e:
        logger.error(f"Something went wrong :( ({e})")
    finally:
        GPIO.output(IR_LED_PIN, GPIO.LOW)

    trigger_end_time = int(time.time() - trigger_start_time)

    logger.debug(f"Trigger Runtime: {trigger_end_time} seconds")
    logger.debug(log_stats())
    logger.info("Ready!")


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGTERM, graceful_exit)

        for i in range(10):
            GPIO.output(IR_LED_PIN, i%2)
            time.sleep(0.5)

        GPIO.output(IR_LED_PIN, GPIO.LOW)

        logger.debug(log_stats())
        logger.info("Ready!")
        
        motion_trigger_action(MOTION_SENSOR_PIN, force=True)

        GPIO.add_event_detect(MOTION_SENSOR_PIN, GPIO.RISING, callback=motion_trigger_action)

        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        graceful_exit()
