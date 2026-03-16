import time
import multiprocessing
from dataclasses import dataclass
from multiprocessing import Queue
from evdev import InputDevice, ecodes, categorize
from cds_py_logger import Logger
from cds_py_hotplug import USB_DEVICES, HOTPLUG
from typing import Any, Callable, Optional


@dataclass
class HidConstants:
    """
    Class contains constant data related to the HID class.
    """

    HID_SETUP_DELAY: int = 1
    SCANCODES = {
        # Scancode: ASCIICode
        0: None,
        1: "ESC",
        2: "1",
        3: "2",
        4: "3",
        5: "4",
        6: "5",
        7: "6",
        8: "7",
        9: "8",
        10: "9",
        11: "0",
        12: "-",
        13: "=",
        14: "BKSP",
        15: "TAB",
        16: "Q",
        17: "W",
        18: "E",
        19: "R",
        20: "T",
        21: "Y",
        22: "U",
        23: "I",
        24: "O",
        25: "P",
        26: "[",
        27: "]",
        28: "CRLF",
        29: "LCTRL",
        30: "A",
        31: "S",
        32: "D",
        33: "F",
        34: "G",
        35: "H",
        36: "J",
        37: "K",
        38: "L",
        39: ";",
        40: '"',
        41: "`",
        42: "LSHFT",
        43: "\\",
        44: "Z",
        45: "X",
        46: "C",
        47: "V",
        48: "B",
        49: "N",
        50: "M",
        51: ",",
        52: ".",
        53: "/",
        54: "RSHFT",
        56: "LALT",
        100: "RALT",
    }
    SCANCODE_FILTERS = {"LSHFT": 42, "RSHFT": 54, "ESC": 1, "CRLF": 28}


class GenericHidReader(multiprocessing.Process):
    """
    Class for managing the Generic HID device. Contains functions for getting data from the device

    Args:
        `data_queue`        : Queue to receive data
        `device_path`       : A string containing the device path
        `config`            : Dictionary containing the HID related configs
        `connection_status` : Queue to receive connection status
    """

    def __init__(
        self,
        data_queue :Queue,
        config: dict[str,Any],
        connection_status: Queue,
        device_name: str = "GenericHidHandler",
        logger: Logger=Logger(logger_name="GenericHidReader"),
    ):
        self._logger = logger
        self._logger.info("HID: Init HID Scanner")

        super().__init__()  # Call the constructor of the superclass (multiprocessing.Process)
        self.is_connected = multiprocessing.Value("b", False)
        self._is_data_ready = False
        self._is_read_aborted = (
            False  # TODO Variable used but never set outside __init__
        )
        self.is_hid_enabled = True

        self.hid_data = ""
        self.new_hid_data = ""
        self.data_queue = data_queue
        self.connection_status = connection_status
        self.hid_device = USB_DEVICES(
            name=device_name,
            vendor_id=config["vid"],
            product_id=config["pid"],
            subsystem="input",
            connection_cb=self._on_connect_callback_handler,
            disconnection_cb=self._on_disconnect_callback_handler,
            logger_object=self._logger,
        )
        self.hotplug_handler = HOTPLUG(logger_object=self._logger)
        self.hotplug_handler.register_device(self.hid_device)
        self._setup_hid()

    def run(self):
        """
        Overrides the beaviour of superclass Process function
        """
        self.hotplug_handler.start()
        self._process_hid_events()

    def _process_hid_events(self):
        """
        Continuously processes HID events for barcode data.
        This function listens for HID events and processes them.
        Handles disconnect/reconnect by waiting for a valid device.
        """
        while True:
            if not self.is_connected.value:
                time.sleep(1)
                continue
            try:
                # Only enter read loop if device is connected and valid
                for event in self.dev.read_loop():
                    if not self.is_connected.value:
                        break  # Device disconnected, break out to wait for reconnection
                    if event.type != ecodes.EV_KEY:
                        continue
                    data = categorize(event)
                    if data.keystate != HidConstants.SCANCODE_FILTERS["ESC"]:
                        continue
                    if (
                        data.scancode == HidConstants.SCANCODE_FILTERS["RSHFT"]
                        or data.scancode == HidConstants.SCANCODE_FILTERS["LSHFT"]
                    ):
                        continue
                    if (
                        data.scancode == HidConstants.SCANCODE_FILTERS["CRLF"]
                        and self.is_hid_enabled
                    ):
                        self._is_data_ready = True
                        self.new_hid_data = self.hid_data
                        self.data_queue.put({"UUID": self.new_hid_data}, block=False)
                        self.hid_data = ""
                    else:
                        self.hid_data += HidConstants.SCANCODES[data.scancode]
            except Exception as error:
                self._logger.warning(f"HID Event Handler Crash, Possible barcode scanner disconnection {error}")
                time.sleep(1)

    def _on_connect_callback_handler(self):
        """
        Callback function for the connection event. This function is called when HID device is connected.
        Sets up the HID device and set it's connection status as connected.
        """
        self._logger.warning("HID Device Reconnected")
        self._setup_hid()
        self.connection_status.put({"isConnected": True}, block=False)

    def _on_disconnect_callback_handler(self):
        """
        Callback function for disconnect event. It is called when the device is disconnected.
        """
        self._logger.warning("HID Device Disconnected")
        self.is_connected.value = False
        self.connection_status.put({"isConnected": False}, block=False)

    def _setup_hid(self):
        """
        Setup the HID device for communication via the USB interface.
        """
        time.sleep(HidConstants.HID_SETUP_DELAY)
        try:
            print("Setting up HID Device...")
            if not self.is_connected.value:
                self._device = self.hid_device.get_device_path()
                print(f"HID Device Path: {self._device}")
                if self._device is not None:
                    self.dev: InputDevice = InputDevice(self._device)
                    print(f"HID Device Info: {self.dev}")
                    self.dev.grab()
                    self.is_connected.value = True
        except Exception as error:
            self._logger.error(f"HID: Error opening port, Err:{error}")
