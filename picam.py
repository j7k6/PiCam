#!/usr/bin/env python3

from PIL import Image
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
import RPi.GPIO as GPIO
import cv2
import datetime
import logging
import math
import os
import picamera
import smtplib
import ssl
import subprocess
import sys
import time
import yaml


with open("config.yml") as stream:
    try:
        config = yaml.safe_load(stream)["config"]
    except yaml.YAMLError as e:
        logging.fatal(e)


""" GPIO Setup """
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(config["gpio"]["ir_led_pin"], GPIO.OUT)
GPIO.setup(config["gpio"]["motion_sensor_pin"], GPIO.IN)
GPIO.setup(config["gpio"]["modem_power_status_pin"], GPIO.IN)
GPIO.setup(config["gpio"]["modem_power_trigger_pin"], GPIO.OUT)
GPIO.setup(config["gpio"]["low_battery_pin"], GPIO.IN)

""" PCamera Setup """
camera = picamera.PiCamera()
camera.rotation = config["camera"]["rotation"]
camera.color_effects = (128, 128)
camera.annotate_background = picamera.Color('black')
camera.annotate_foreground = picamera.Color('white')


class PPP:
    def __init__(self):
        self.proc = None
        self.connected = False

    def connect(self):
        self.proc = subprocess.Popen(config["modem"]["ppp_call_command"].split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        for i in range(config["modem"]["ppp_timeout"]):
            with open("/proc/net/dev") as fp:
                for line in fp:
                    if line.split()[0].startswith("ppp0") and int(line.split()[2]) > 0:
                        self.connected = True

            time.sleep(1)

            if self.connected:
                break

    def disconnect(self):
        try:
            self.proc.kill()
        except Exception as e:
            pass

        try:
            os.remove("/var/lock/LCK..ttyAMA0")
        except Exception as e:
            pass


def generate_video_thumbnails(video_path, video_thumbnails_path):
    video_thumbnails_generated = False

    try:
        cap = cv2.VideoCapture(video_path)

        frames_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frames_per_col = int(math.sqrt(config["camera"]["video_thumbnails_num"]))
        frames_per_thumbnail = int(frames_total/config["camera"]["video_thumbnails_num"])

        w, h = tuple(config["camera"]["preview_res"])
        video_thumbnails_image = Image.new("RGBA", (w*frames_per_col, h*frames_per_col), 255)

        cur, row, col = 0, 0, 0

        for i in range(config["camera"]["video_thumbnails_num"]):
            cap.set(1, cur)
            ret, frame = cap.read()

            video_thumbnails_image.paste(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), (w*col, h*row))

            cur += frames_per_thumbnail

            if (i+1) % frames_per_col == 0:
                row += 1
            if (col+1) % frames_per_col == 0:
                col = 0
            else:
                col += 1

        cap.release()

        video_thumbnails_image.convert("RGB").save(video_thumbnails_path)

        video_thumbnails_generated = True
    except Exception as e:
        logging.error(e)
        pass

    return video_thumbnails_generated


def capture_photo(photo_path):
    photo_captured = False

    camera.resolution = tuple(config["camera"]["photo_res"])
    camera.annotate_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    camera.annotate_text_size = 40

    try:
        camera.capture(photo_path)

        photo_captured = True
    except Exception as e:
        logging.error(e)
        pass

    return photo_captured


def capture_video(video_path):
    video_captured = False
    video_tmp_path = os.path.join("/tmp", f"{os.path.splitext(os.path.basename(video_path))[0]}.h264")

    camera.resolution = tuple(config["camera"]["preview_res"])
    camera.annotate_text_size = 16
    camera.annotate_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        camera.start_recording(video_tmp_path, format="h264", quality=23)
        start_time = datetime.datetime.now()

        while (datetime.datetime.now()-start_time).total_seconds() < config["camera"]["video_max_length"]:
            if int((datetime.datetime.now()-start_time).total_seconds()) % 5 == 0 and GPIO.input(config["gpio"]["motion_sensor_pin"]) == 0:
                break

            camera.annotate_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            camera.wait_recording(1)

        camera.stop_recording()

        if subprocess.call([config["base"]["ffmpeg_path"], "-i", video_tmp_path, "-c", "copy", video_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            video_captured = True
    except Exception as e:
        logging.error(e)
        pass

    try:
        os.remove(video_tmp_path)
    except Exception as e:
        pass

    return video_captured


def send_mail(previews):
    mail_sent = False
    smtp_connected = False

    message = MIMEMultipart()
    message["From"] = config["smtp"]["from"]
    message["To"] = config["smtp"]["to"]
    message["Subject"] = "PiCam Triggered!"

    if not bool(GPIO.input(config["gpio"]["low_battery_pin"])):
        message.attach(MIMEText("LOW BATTERY!", "plain"))

    for preview in previews:
        try:
            preview_buf = BytesIO()
            Image.open(preview).resize(tuple(config["camera"]["preview_res"]), Image.ANTIALIAS).save(preview_buf, format="JPEG")

            part = MIMEBase("application", "octet-stream")
            part.set_payload(preview_buf.getvalue())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename= {os.path.basename(preview)}")
            message.attach(part)
        except Exception as e:
            logging.error(e)
            pass
        finally:
            preview_buf.close()

    try:
        server = smtplib.SMTP(config["smtp"]["server"], config["smtp"]["port"])
        smtp_connected = True
    except Exception as e:
        logging.error(e)
        pass

    if smtp_connected:
        try:
            if config["smtp"]["starttls"]:
                server.starttls(context=ssl.create_default_context())

            try:
                server.login(config["smtp"]["username"], config["smtp"]["password"])
            except Exception as e:
                pass

            server.sendmail(config["smtp"]["from"], config["smtp"]["to"], message.as_string())

            mail_sent = True
        except Exception as e:
            logging.error(e)
            pass
        finally:
            server.quit()

    return mail_sent


def modem_trigger_action(power_status):
    if power_status != GPIO.input(config["gpio"]["modem_power_status_pin"]):
        while power_status != GPIO.input(config["gpio"]["modem_power_status_pin"]):
            GPIO.output(config["gpio"]["modem_power_trigger_pin"], GPIO.HIGH)
            time.sleep(2)
            GPIO.output(config["gpio"]["modem_power_trigger_pin"], GPIO.LOW)
            time.sleep(2)

            if power_status == GPIO.input(config["gpio"]["modem_power_status_pin"]):
                break

        time.sleep(2)


def motion_trigger_action(channel, force=False):
    logging.info("Motion Sensor triggered!")

    if GPIO.input(channel) == 0 and force is False:
        logging.debug("False Alarm!")
        return
    else:
        logging.info(f"Capturing Photo...")

        GPIO.output(config["gpio"]["ir_led_pin"], GPIO.HIGH)
        time.sleep(2)

        previews = []
        photo_path = os.path.join(config["base"]["data_dir"], f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.jpg")

        if capture_photo(photo_path):
            logging.debug(f"Photo Captured: {photo_path}")
            previews.append(photo_path)

        time.sleep(2)

        video_captured = False

        if GPIO.input(channel) == 1:
            logging.info(f"Capturing Video...")
            video_path = os.path.join(config["base"]["data_dir"], f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.mp4")
            video_captured = capture_video(video_path)

        GPIO.output(config["gpio"]["ir_led_pin"], GPIO.LOW)

        if video_captured:
            logging.debug(f"Video Captured: {video_path}")

            logging.info(f"Generating Video Thumbnails...")
            video_thumbnails_generated = False
            video_thumbnails_path = os.path.join("/tmp", f"{os.path.splitext(os.path.basename(video_path))[0]}.jpg")

            if generate_video_thumbnails(video_path, video_thumbnails_path):
                logging.debug(f"Video Thumbnails Generated: {video_thumbnails_path}")
                previews.append(video_thumbnails_path)

        if len(previews) > 0:
            logging.info("Starting Modem...")
            modem_trigger_action(1)

            logging.info("Connecting PPP...")
            ppp = PPP()
            ppp.connect()

            if ppp.connected:
                logging.info("Sending Preview...")

                if send_mail(previews) is False:
                    logging.error("SMTP Error! Mail not sent!")
            else:
                logging.error("PPP Connection Error!")

            ppp.disconnect()

            logging.info("Stopping Modem...")
            modem_trigger_action(0)

            logging.info("Done!")

        logging.debug(f"Sleeping for {config['base']['wait_time']} seconds...")
        time.sleep(config["base"]["wait_time"])
        logging.debug("Ready!")


if __name__ == "__main__":
    if sys.stdout.isatty():
        logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.DEBUG)
    else:
        logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO, handlers=[logging.FileHandler(os.path.join(config["base"]["data_dir"], "picam.log"))])

    logging.info("Ready!")
    
    motion_trigger_action(config["gpio"]["motion_sensor_pin"], force=True)

    try:
        GPIO.add_event_detect(config["gpio"]["motion_sensor_pin"], GPIO.RISING, callback=motion_trigger_action)

        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logging.info("Exiting...")

        GPIO.output(config["gpio"]["ir_led_pin"], GPIO.LOW)
        camera.close()
        modem_trigger_action(0)
        GPIO.cleanup()
