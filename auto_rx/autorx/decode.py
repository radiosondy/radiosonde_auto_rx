#!/usr/bin/env python
#
#   radiosonde_auto_rx - Sonde Decoder Class.
#
#   Copyright (C) 2018  Mark Jessop <vk5qi@rfhead.net>
#   Released under GNU GPL v3 or later
#
import logging
import json
import os
import signal
import subprocess
import time
import traceback
from dateutil.parser import parse
from threading import Thread
from types import FunctionType, MethodType
from .utils import AsynchronousFileReader, rtlsdr_test
from .gps import get_ephemeris, get_almanac
from .sonde_specific import *

# Global valid sonde types list.
VALID_SONDE_TYPES = ['RS92', 'RS41', 'DFM', 'M10', 'iMet']


class SondeDecoder(object):
    '''
    Radiosonde Sonde Decoder class. Run a radiosonde decoder program as a subprocess, and pass the output onto exporters.

    Notes:
    The sonde decoder binary needs to output telemetry data as a valid JSON, with one frame of telemetry per line.
    Example:
    { "frame": 1909, "id": "N3710309", "datetime": "2018-05-12T11:32:20.000Z", "lat": -34.90842, "lon": 138.49243, "alt": 3896.43871, "vel_h": 8.60708, "heading": 342.43237, "vel_v": 6.83107 }
    Required Fields are:
        "frame" (int):  Frame counter. Usually provided by the radiosonde, and increments per telemetry frame.
        "id" (str):     Unique identifier for the radiosonde, usually some kind of serial number.
        "datetime" (str): UTC Date/Time string, which indicates the time applicable to the telemetry sentence.
            Must be parseable with dateutil. 
        "lat" (float):  Radiosonde Latitude (decmial degrees)
        "lon" (float):  Radiosonde Longitude (decimal degrees)
        "alt" (float):  Radiosonde Altitude (metres)
    Optional Fields:
        These fields will be set to dummy values if they are not provided within the JSON blob.
        "temp" (float): Atmospheric temperature reported by the Radiosonde (degrees Celsius)
        "humidity" (float): Humidity value, reported by the radiosonde (%)
        "vel_h" (float): Horizontal Velocity (metres/s)
        "vel_v" (float): Vertical Velocity (metres/s)
        "heading" (float): Heading of the movement of the payload (degrees true)
    The following fields are added to the dictionary:
        "type" (str): Radiosonde type
        "freq_float" (float): Radiosonde frequency in MHz, as a float.
        "freq" (str): Radiosonde frequency as a string (XXX.XXX MHz).
        "datetime_dt" (datetime): Telemetry sentence time, as a datetime object.
    '''

    DECODER_REQUIRED_FIELDS = ['frame', 'id', 'datetime', 'lat', 'lon', 'alt']
    DECODER_OPTIONAL_FIELDS = {
        'temp'      : -273.0,
        'humidity'  : -1,
        'batt'      : -1,
        'vel_h'     : 0.0,
        'vel_v'     : 0.0,
        'heading'   : 0.0
    }

    VALID_SONDE_TYPES = ['RS92', 'RS41', 'DFM', 'M10', 'iMet']

    def __init__(self,
        sonde_type="None",
        sonde_freq=400000000.0,
        rs_path = "./",
        sdr_fm = "rtl_fm",
        device_idx = 0,
        ppm = 0,
        gain = -1,
        bias = False,
        save_decode_audio = False,
        save_decode_iq = False,

        exporter = None,
        timeout = 180,
        telem_filter = None,

        rs92_ephemeris = None,

        imet_location = ""):
        """ Initialise and start a Sonde Decoder.

        Args:
            sonde_type (str): The radiosonde type, as returned by SondeScanner. Valid types listed in VALID_SONDE_TYPES
            sonde_freq (int/float): The radiosonde frequency, in Hz.
            
            rs_path (str): Path to the RS binaries (i.e rs_detect). Defaults to ./
            sdr_fm (str): Path to rtl_fm, or drop-in equivalent. Defaults to 'rtl_fm'
            device_idx (int or str): Device index or serial number of the RTLSDR. Defaults to 0 (the first SDR found).
            ppm (int): SDR Frequency accuracy correction, in ppm.
            gain (int): SDR Gain setting, in dB. A gain setting of -1 enables the RTLSDR AGC.
            bias (bool): If True, enable the bias tee on the SDR.

            save_decode_audio (bool): If True, save the FM-demodulated audio to disk to decode_<device_idx>.wav.
                                      Note: This may use up a lot of disk space!
            save_decode_iq (bool): If True, save the decimated IQ stream (48 or 96k complex s16 samples) to disk to decode_IQ_<device_idx>.bin
                                      Note: This will use up a lot of disk space!

            exporter (function, list): Either a function, or a list of functions, which accept a single dictionary. Fields described above.
            timeout (int): Timeout after X seconds of no valid data received from the decoder. Defaults to 180.
            telem_filter (function): An optional filter function, which determines if a telemetry frame is valid. 
                This can be used to allow the decoder to timeout based on telemetry contents (i.e. no lock, too far away, etc), 
                not just lack-of-telemetry. This function is passed the telemetry dict, and must return a boolean based on the telemetry validity.

            rs92_ephemeris (str): OPTIONAL - A fixed ephemeris file to use if decoding a RS92. If not supplied, one will be downloaded.

            imet_location (str): OPTIONAL - A location field which is use in the generation of iMet unique ID.
        """
        # Thread running flag
        self.decoder_running = True

        # Local copy of init arguments
        self.sonde_type = sonde_type
        self.sonde_freq = sonde_freq

        self.rs_path = rs_path
        self.sdr_fm = sdr_fm
        self.device_idx = device_idx
        self.ppm = ppm
        self.gain = gain
        self.bias = bias
        self.save_decode_audio = save_decode_audio
        self.save_decode_iq = save_decode_iq

        self.telem_filter = telem_filter
        self.timeout = timeout
        self.rs92_ephemeris = rs92_ephemeris
        self.imet_location = imet_location

        # iMet ID store. We latch in the first iMet ID we calculate, to avoid issues with iMet-1-RS units
        # which don't necessarily have a consistent packet count to time increment ratio.
        # This is a tradeoff between being able to handle multiple iMet sondes on a single frequency, and
        # not flooding the various databases with sonde IDs in the case of a bad sonde.
        self.imet_id = None

        # This will become our decoder thread.
        self.decoder = None

        self.exit_state = "OK"

        # Detect if we have an 'inverted' sonde.
        if self.sonde_type.startswith('-'):
            self.inverted = True
            # Strip off the leading '-' character'
            self.sonde_type = self.sonde_type[1:]
        else:
            self.inverted = False

        # Check if the sonde type is valid.
        if self.sonde_type not in self.VALID_SONDE_TYPES:
            self.log_error("Unsupported sonde type: %s" % self.sonde_type)
            self.decoder_running = False
            return 

        # Test if the supplied RTLSDR is working.
        _rtlsdr_ok = rtlsdr_test(device_idx)

        # TODO: How should this error be handled?
        if not _rtlsdr_ok:
            self.log_error("RTLSDR #%s non-functional - exiting." % device_idx)
            self.decoder_running = False
            return

        # We can accept a few different types in the exporter argument..
        # Nothing...
        if exporter == None:
            self.exporters = None

        # A single function...
        elif type(exporter) == FunctionType:
            self.exporters = [exporter]

        # A list of functions...
        elif type(exporter) == list:
            # Check everything in the list is a function
            for _func in exporter:
                if (type(_func) is not FunctionType) and (type(_func) is not MethodType):
                    raise TypeError("Supplied exporter list does not contain functions.")
            
            # If it all checks out, use the supplied list.
            self.exporters = exporter
            
        else:
            # Otherwise, bomb out. 
            raise TypeError("Supplied exporter has incorrect type.")

        # Generate the decoder command.
        self.decoder_command = self.generate_decoder_command()

        if self.decoder_command is None:
            self.log_error("Could not generate decoder command. Not starting decoder.")
            self.decoder_running = False
        else:
            # Start up the decoder thread.
            self.decode_process = None
            self.async_reader = None

            self.decoder_running = True
            self.decoder = Thread(target=self.decoder_thread)
            self.decoder.start()


    def generate_decoder_command(self):
        """ Generate the shell command which runs the relevant radiosonde decoder.

        This is where support for new sonde types can be added.s

        Returns:
            str/None: The shell command which will be run in the decoder thread, or none if a valid decoder could not be found.

        """
        # Common options to rtl_fm

        # Add a -T option if bias is enabled
        bias_option = "-T " if self.bias else ""

        # Add a gain parameter if we have been provided one.
        if self.gain != -1:
            gain_param = '-g %.1f ' % self.gain
        else:
            gain_param = ''


        if self.sonde_type == "RS41":
            # RS41 Decoder command.
            # rtl_fm -p 0 -g -1 -M fm -F9 -s 15k -f 405500000 | sox -t raw -r 15k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - lowpass 2600 2>/dev/null | ./rs41ecc --crc --ecc --ptu
            # Note: Have removed a 'highpass 20' filter from the sox line, will need to re-evaluate if adding that is useful in the future.
            decode_cmd = "%s %s-p %d -d %s %s-M fm -F9 -s 15k -f %d 2>/dev/null |" % (self.sdr_fm, bias_option, int(self.ppm), str(self.device_idx), gain_param, self.sonde_freq)
            decode_cmd += "sox -t raw -r 15k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - lowpass 2600 2>/dev/null |"

            # Add in tee command to save audio to disk if debugging is enabled.
            if self.save_decode_audio:
                decode_cmd += " tee decode_%s.wav |" % str(self.device_idx)

            decode_cmd += "./rs41mod --ptu --json 2>/dev/null"

        elif self.sonde_type == "RS92":
            # Decoding a RS92 requires either an ephemeris or an almanac file.
            # If we have been supplied an ephemeris file, we will attempt to use it, otherwise
            # we will try and download one.
            if self.rs92_ephemeris == None:
                # If no ephemeris data defined, attempt to download it.
                # get_ephemeris will either return the saved file name, or None.
                self.rs92_ephemeris = get_ephemeris(destination="ephemeris.dat")

                # If ephemeris is still None, then we failed to download the ephemeris data.
                # Try and grab the almanac data instead
                if self.rs92_ephemeris == None:
                    self.log_error("Could not obtain ephemeris data, trying to download an almanac.")
                    almanac = get_almanac(destination="almanac.txt")
                    if almanac == None:
                        # We probably don't have an internet connection. Bomb out, since we can't do much with the sonde telemetry without an almanac!
                        self.log_error("Could not obtain GPS ephemeris or almanac data.")
                        return None
                    else:
                        _rs92_gps_data = "-a almanac.txt --gpsepoch 2" # Note - This will need to be updated in... 19 years.
                else:
                    _rs92_gps_data = "-e ephemeris.dat"
            else:
                _rs92_gps_data = "-e %s" % self.rs92_ephemeris

            # Adjust the receive bandwidth based on the band the scanning is occuring in.
            if self.sonde_freq < 1000e6:
                # 400-406 MHz sondes - use a 12 kHz FM demod bandwidth.
                _rx_bw = 12000
            else:
                # 1680 MHz sondes - use a 28 kHz FM demod bandwidth.
                # NOTE: This is a first-pass of this bandwidth, and may need to be optimized.
                _rx_bw = 28000

            # Now construct the decoder command.
            # rtl_fm -p 0 -g 26.0 -M fm -F9 -s 12k -f 400500000 | sox -t raw -r 12k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - highpass 20 lowpass 2500 2>/dev/null | ./rs92ecc -vx -v --crc --ecc --vel -e ephemeris.dat
            decode_cmd = "%s %s-p %d -d %s %s-M fm -F9 -s %d -f %d 2>/dev/null |" % (self.sdr_fm, bias_option, int(self.ppm), str(self.device_idx), gain_param, _rx_bw, self.sonde_freq)
            decode_cmd += "sox -t raw -r %d -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - lowpass 2500 highpass 20 2>/dev/null |" % _rx_bw

            # Add in tee command to save audio to disk if debugging is enabled.
            if self.save_decode_audio:
                decode_cmd += " tee decode_%s.wav |" % str(self.device_idx)

            decode_cmd += "./rs92mod -vx -v --crc --ecc --vel --json %s 2>/dev/null" % _rs92_gps_data

        elif self.sonde_type == "DFM":
            # DFM06/DFM09 Sondes.
            # As of 2019-02-10, dfm09ecc auto-detects if the signal is inverted,
            # so we don't need to specify an invert flag.
            # 2019-02-27: Added the --dist flag, which should reduce bad positions a bit.

            # Note: Have removed a 'highpass 20' filter from the sox line, will need to re-evaluate if adding that is useful in the future.
            decode_cmd = "%s %s-p %d -d %s %s-M fm -F9 -s 15k -f %d 2>/dev/null |" % (self.sdr_fm, bias_option, int(self.ppm), str(self.device_idx), gain_param, self.sonde_freq)
            decode_cmd += "sox -t raw -r 15k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - highpass 20 lowpass 2000 2>/dev/null |"

            # Add in tee command to save audio to disk if debugging is enabled.
            if self.save_decode_audio:
                decode_cmd += " tee decode_%s.wav |" % str(self.device_idx)

            # DFM decoder
            decode_cmd += "./dfm09ecc -vv --ecc --json --dist --auto 2>/dev/null"
			
        elif self.sonde_type == "M10":
            # M10 Sondes

            decode_cmd = "%s %s-p %d -d %s %s-M fm -F9 -s 22k -f %d 2>/dev/null |" % (self.sdr_fm, bias_option, int(self.ppm), str(self.device_idx), gain_param, self.sonde_freq)
            decode_cmd += "sox -t raw -r 22k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - highpass 20 2>/dev/null |"

            # Add in tee command to save audio to disk if debugging is enabled.
            if self.save_decode_audio:
                decode_cmd += " tee decode_%s.wav |" % str(self.device_idx)

            # M10 decoder
            decode_cmd += "./m10 -b -b2 2>/dev/null"

        elif self.sonde_type == "iMet":
            # iMet-4 Sondes

            decode_cmd = "%s %s-p %d -d %s %s-M fm -F9 -s 15k -f %d 2>/dev/null |" % (self.sdr_fm, bias_option, int(self.ppm), str(self.device_idx), gain_param, self.sonde_freq)
            decode_cmd += "sox -t raw -r 15k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - highpass 20 2>/dev/null |"

            # Add in tee command to save audio to disk if debugging is enabled.
            if self.save_decode_audio:
                decode_cmd += " tee decode_%s.wav |" % str(self.device_idx)

            # iMet-4 (IMET1RS) decoder
            decode_cmd += "./imet1rs_dft --json 2>/dev/null"

        else:
            # Should never get here.
            return None

        return decode_cmd


    def decoder_thread(self):
        """ Runs the supplied decoder command as a subprocess, and passes returned lines to handle_decoder_line. """
        
        # Timeout Counter. 
        _last_packet = time.time()

        self.log_debug("Decoder Command: %s" % self.decoder_command )

        # Start the thread.
        self.decode_process = subprocess.Popen(self.decoder_command, shell=True, stdin=None, stdout=subprocess.PIPE, preexec_fn=os.setsid) 
        self.async_reader = AsynchronousFileReader(self.decode_process.stdout, autostart=True)

        self.log_info("Starting decoder subprocess.")

        while (not self.async_reader.eof()) and self.decoder_running:
            # Read in any lines available in the async reader queue.
            for _line in self.async_reader.readlines():
                if (_line != None) and (_line != ""):
                    # Pass the line into the handler, and see if it is OK.
                    _ok = self.handle_decoder_line(_line)

                    # If we decoded a valid JSON blob, update our last-packet time.
                    if _ok:
                        _last_packet = time.time()


            # Check timeout counter.
            if time.time() > (_last_packet + self.timeout):
                # If we have not seen data for a while, break.
                self.log_error("RX Timed out.")
                self.exit_state = "Timeout"
                break
            else:
                # Otherwise, sleep for a short time.
                time.sleep(0.1)

        # Either our subprocess has exited, or the user has asked to close the process. 
        #Try many things to kill off the subprocess.
        try:
            # Stop the async reader
            self.async_reader.stop()
            # Send a SIGKILL to the subprocess PID via OS.
            try:
                os.killpg(os.getpgid(self.decode_process.pid), signal.SIGKILL)
            except Exception as e:
                self.log_debug("SIGKILL via os.killpg failed. - %s" % str(e))
            time.sleep(1)
            try:
                # Send a SIGKILL via subprocess
                self.decode_process.kill()
            except Exception as e:
                self.log_debug("SIGKILL via subprocess.kill failed - %s" % str(e))
            # Finally, join the async reader.
            self.async_reader.join()
            

        except Exception as e:
            traceback.print_exc()
            self.log_error("Error while killing subprocess - %s" % str(e))

        self.log_info("Closed decoder subprocess.")
        self.decoder_running = False


    def handle_decoder_line(self, data):
        """ Handle a line of output from the decoder subprocess.

        Args:
            data (str, bytearray): One line of text output from the decoder subprocess.

        Returns:
            bool:   True if the line was decoded to a JSON object correctly, False otherwise.
        """

        # Don't even try and decode lines which don't start with a '{'
        # These may be other output from the decoder, which we shouldn't try to parse.
        
        # Catch 'bad' first characters.
        try:
            _first_char = data.decode('ascii')[0]
        except UnicodeDecodeError:
            return

        # Catch non-JSON object lines.
        if data.decode('ascii')[0] != '{':
            return

        else:
            try:
                _telemetry = json.loads(data.decode('ascii'))
            except Exception as e:
                self.log_debug("Line could not be parsed as JSON - %s" % str(e))
                return False

            # Check the JSON blob has been parsed as a dictionary
            if type(_telemetry) is not dict:
                self.log_error("Parsed JSON object is not a dictionary!")
                return False

            # Check that the required fields are in the telemetry blob
            for _field in self.DECODER_REQUIRED_FIELDS:
                if _field not in _telemetry:
                    self.log_error("JSON object missing required field %s" % _field)
                    return False

            # Check for optional fields, and add them if necessary.
            for _field in self.DECODER_OPTIONAL_FIELDS.keys():
                if _field not in _telemetry:
                    _telemetry[_field] = self.DECODER_OPTIONAL_FIELDS[_field]


            # Check for an encrypted flag (this indicates a sonde that we cannot decode telemetry from.)
            if 'encrypted' in _telemetry:
                self.log_error("Radiosonde %s has encrypted telemetry (possible RS41-SGM)! We cannot decode this, closing decoder." % _telemetry['id'])
                self.exit_state = "Encrypted"
                self.decoder_running = False
                return False

            # Check the datetime field is parseable.
            try:
                _telemetry['datetime_dt'] = parse(_telemetry['datetime'])
            except Exception as e:
                self.log_error("Invalid date/time in telemetry dict - %s (Sonde may not have GPS lock)" % str(e))
                return False

            # Add in the sonde frequency and type fields.
            _telemetry['type'] = self.sonde_type
            _telemetry['freq_float'] = self.sonde_freq/1e6
            _telemetry['freq'] = "%.3f MHz" % (self.sonde_freq/1e6)

            # Add in information about the SDR used.
            _telemetry['sdr_device_idx'] = self.device_idx

            # Check for an 'aux' field, this indicates that the sonde has an auxilliary payload,
            # which is most likely an Ozone sensor. We append -Ozone to the sonde type field to indicate this.
            if 'aux' in _telemetry:
                _telemetry['type'] += "-Ozone"


            # iMet Specific actions
            if self.sonde_type == 'iMet':
                # Check we have GPS lock.
                if _telemetry['sats'] < 4:
                    # No GPS lock means an invalid time, which means we can't accurately calculate a unique ID.
                    self.log_error("iMet sonde has no GPS lock - discarding frame.")
                    return False

                # Fix up the time.
                _telemetry['datetime_dt'] = imet_fix_datetime(_telemetry['datetime'])
                # Generate a unique ID based on the power-on time and frequency, as iMet sondes don't send one.
                # Latch this ID and re-use it for the entire decode run.
                if self.imet_id == None:
                    self.imet_id = imet_unique_id(_telemetry, custom=self.imet_location)
                
                _telemetry['id'] = self.imet_id
                _telemetry['station_code'] = self.imet_location

            # If we have been provided a telemetry filter function, pass the telemetry data
            # through the filter, and return the response
            # By default, we will assume the telemetry is OK.
            _telem_ok = True
            if self.telem_filter is not None:
                try:
                    _telem_ok = self.telem_filter(_telemetry)
                except Exception as e:
                    self.log_error("Failed to run telemetry filter - %s" % str(e))
                    _telem_ok = True


            # If the telemetry is OK, send to the exporter functions (if we have any).
            if self.exporters is None:
                return
            else:
                if _telem_ok:
                    for _exporter in self.exporters:
                        try:
                            _exporter(_telemetry)
                        except Exception as e:
                            self.log_error("Exporter Error %s" % str(e))

            return _telem_ok




    def log_debug(self, line):
        """ Helper function to log a debug message with a descriptive heading. 
        Args:
            line (str): Message to be logged.
        """
        logging.debug("Decoder #%s %s %.3f - %s" % (str(self.device_idx), self.sonde_type, self.sonde_freq/1e6, line))


    def log_info(self, line):
        """ Helper function to log an informational message with a descriptive heading. 
        Args:
            line (str): Message to be logged.
        """
        logging.info("Decoder #%s %s %.3f - %s" % (str(self.device_idx), self.sonde_type, self.sonde_freq/1e6, line))


    def log_error(self, line):
        """ Helper function to log an error message with a descriptive heading. 
        Args:
            line (str): Message to be logged.
        """
        logging.error("Decoder #%s %s %.3f - %s" % (str(self.device_idx), self.sonde_type, self.sonde_freq/1e6, line))


    def stop(self):
        """ Kill the currently running decoder subprocess """
        self.decoder_running = False

        if self.decoder is not None:
            self.decoder.join()


    def running(self):
        """ Check if the decoder subprocess is running. 

        Returns:
            bool: True if the decoder subprocess is running.
        """
        return self.decoder_running


if __name__ == "__main__":
    # Test script.
    from .logger import TelemetryLogger
    from .habitat import HabitatUploader

    logging.basicConfig(format='%(asctime)s %(levelname)s:%(message)s', level=logging.DEBUG)
    # Make requests & urllib3 STFU
    requests_log = logging.getLogger("requests")
    requests_log.setLevel(logging.CRITICAL)
    urllib3_log = logging.getLogger("urllib3")
    urllib3_log.setLevel(logging.CRITICAL)


    _log = TelemetryLogger(log_directory="./testlog/")
    _habitat = HabitatUploader(user_callsign="VK5QI_AUTO_RX_DEV", inhibit=False)

    try:
        _decoder = SondeDecoder(sonde_freq = 401.5*1e6,
            sonde_type = "RS41",
            timeout = 50,
            device_idx="00000002",
            exporter=[_habitat.add, _log.add])

        # _decoder2 = SondeDecoder(sonde_freq = 405.5*1e6,
        #     sonde_type = "RS41",
        #     timeout = 50,
        #     device_idx="00000001",
        #     exporter=[_habitat.add, _log.add])

        while True:
            time.sleep(5)
            if not _decoder.running():
                break
    except KeyboardInterrupt:
        _decoder.stop()
        #_decoder2.stop()
    except:
        traceback.print_exc()
        pass
    
    _habitat.close()
    _log.close()



