#!/usr/bin/python
# Copyright (c) 2021 Arista Networks, Inc.  All rights reserved.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the COPYING file.

import base64
import datetime
import json
import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import time

##############  USER INPUT  ##############
# Note: If you are saving the file on windows, please make sure to use linux (LF) as newline.
# By default, windows uses (CR LF), you need to convert the newline char to linux (LF).

# address for CVaaS, usually just "www.arista.io"
cvAddr = ""

# enrollment token to be copied from CVaaS Device Registration page
enrollmentToken = ""

# Add proxy url if device is behind proxy server, leave it as an empty string otherwise
cvproxy = ""

# eosUrl is an optional parameter, which needs to be added only if the EOS version
# is <4.24, in which case, SysDbHelperUtils is not present on the device.
# This needs to be a http URL pointing to a SWI image on the local network.
eosUrl = ""

'''
Specify the address of the ntp server that the bootstrap script must configure on the device,
which is to sync the clock before it reaches out to CV.
For example:
ntpServer = "ntp1.aristanetworks.com"
'''
ntpServer = ""


##############  CONSTANTS  ##############
SECURE_HTTPS_PORT = "443"
SECURE_TOKEN = "token-secure"
INGEST_TOKEN = "token"
TOKEN_FILE_PATH = "/tmp/token.tok"
BOOT_SCRIPT_PATH = "/tmp/bootstrap-script"
REDIRECTOR_PATH = "api/v3/services/arista.redirector.v1.AssignmentService/GetOne"
VERSION = "1.0.0"

##############  HELPER FUNCTIONS  ##############
proxies = { "https" : cvproxy, "http" : cvproxy }

logger = None
def setupLogger():
   global logger
   logger = logging.getLogger("customBootstrap")
   logger.setLevel(logging.DEBUG)
   try:
      handler = logging.handlers.SysLogHandler(address='/dev/log')
      logger.addHandler(handler)
   except socket.error:
      print( "error setting up logger" )
      logger = None

def log(msg):
   """ Print message to terminal and log if logging is up"""
   print( msg )
   if logger:
      logger.critical(msg)

def monitorNtpSync():
   timeInterval = 10
   expo = 2
   for i in range(5):
      log("Polling NTP status.")
      try:
         ntpStatInfo = subprocess.call(["ntpstat"])
      except:
         raise Exception("ntpstat command failed. Aborting")
      log("NTP sync status - %s" % str(ntpStatInfo))
      if ntpStatInfo == 0:
         log("NTP sync complete.")
         return
      time.sleep(timeInterval)
      timeInterval *= expo
   raise Exception("NTP sync failed. Timing out.")

def getExpiryFromToken(token):
   try:
      # jwt token has 3 parts (header, payload, sign) seperated by a '.'
      # payload has 'exp' field which contains the token expiry time in epoch
      token_payload = token.split(".")[1]
      token_payload_decoded = str(base64.b64decode(token_payload + "==").decode("utf-8"))
      payload = json.loads(token_payload_decoded)
      return payload["exp"], True
   except:
      log("Could not parse the enrollment token. Continuing with ZTP.")
      return -1, False

# Class is used to execute commands in EOS shell
class CliManager(object):
   FAST_CLI_BINARY = "/usr/bin/FastCli"
   def __init__(self):
      self.fastCliBinary = CliManager.FAST_CLI_BINARY
      self.confidenceCheck()

   def confidenceCheck(self):
      assert os.path.isfile(self.fastCliBinary), "FastCli Binary Not Found"

   def runCommands(self, cmdList):
      cmdOutput = ""
      rc = 0
      errMsg = ""
      try:
         cmds = "\n".join(cmdList)
         cmdOutput = subprocess.check_output(
            "echo -e '" + cmds + "' | " + self.fastCliBinary, shell=True, stderr=subprocess.STDOUT,
            universal_newlines=True )
      except subprocess.CalledProcessError as e:
         rc = e.returncode
         errMsg = e.output
         log("Error running commands %s errMsg %s" % (cmds, errMsg))
         return (rc, errMsg)

      if cmdOutput:
         for line in cmdOutput.split('\n'):
            if line.startswith('%'):
               errMsg = cmdOutput
               log("Error running commands %s errMsg %s" % (cmds, errMsg))
               return(1, errMsg)
      return (0, cmdOutput)

# stops and restarts ntp with a specified ntp server
def configureAndRestartNTP(ntpServer):
   cli = CliManager()

   # Command to stop the ntp process
   stopNtp = [ 'en', 'configure', 'no ntp', 'exit' ]
   output, err = cli.runCommands( stopNtp )
   if output != 0:
      log("Trying to run commands [en, configure, no ntp, exit]")
      log("Output: %s, Error %s" % (str(output), str(err)))
      log("NTP server could not be stopped. Aborting")
      raise Exception("NTP server could not be stopped, error:{}. Aborting".format(str(err)))

   # Command to configure and restart ntp process.
   # Note: iburst flag is added for faster synchronization
   configNtp = ['en','configure', 'ntp server {} prefer iburst'.format(ntpServer),'exit']
   output, err = cli.runCommands(configNtp)

   if output != 0:
      log("Trying to run commands [en, configure, ntp server {} prefer iburst exit".format(ntpServer))
      log("Output: %s, Error %s" % (str(output), str(err)))
      log("Could not restart NTP server. Aborting")
      raise Exception("Could not restart NTP server, output:{}. Aborting".format(str(err)))

   # polls and monitors ntpstat command for
   # synchronization status with intervals
   monitorNtpSync()


# Given a filepath and a key, getValueFromFile searches for key=VALUE in it
# and returns the found value without any whitespaces. In case no key specified,
# gives the first string in the first line of the file.
def getValueFromFile( filename, key ):
   if not key:
      with open( filename, "r" ) as f:
         return f.readline().split()[ 0 ]
   else:
      with open( filename, "r" ) as f:
         lines = f.readlines()
         for line in lines:
            if key in line :
               return line.split( "=" )[ 1 ].rstrip( "\n" )
   return None


def tryImageUpgrade( e ):
   # Raise import error if eosUrl is empty
   if eosUrl == "":
      log("Specify eosUrl for EOS version upgrade")
      raise( e )
   subprocess.call( [ "mv", "/mnt/flash/EOS.swi", "/mnt/flash/EOS.swi.bak" ] )
   try:
      cmd = "wget {} -O /mnt/flash/EOS.swi; sudo ip netns exec default /usr/bin/FastCli \
         -p15 -G -A -c $'configure\nboot system flash:/EOS.swi'".format(eosUrl)
      subprocess.check_output( cmd, shell=True, stderr=subprocess.STDOUT )
   except subprocess.CalledProcessError as err:
      # If the link to eosUrl specified is incorrect, then revert back to the older version
      subprocess.call( [ "mv", "/mnt/flash/EOS.swi.bak", "/mnt/flash/EOS.swi" ] )
      log("Failed to download swi from %s, err - %s" % (eosUrl, err.output))
      log("Falling back to original version")
      raise( err )
   subprocess.call( [ "rm", "/mnt/flash/EOS.swi.bak" ] )
   subprocess.call( [ "reboot" ] )


###################  MAIN SCRIPT  ###################

##########  IMPORT HANDLING  ##########
# Some or more of these imports could fail when running this script
# in python2 environment starting EOS 4.30.1 If that is the case, we try to run the
# script with python3. In case we cannot recover, the script will require "eosUrl" to
# perform an upgrade before it can proceed.
try:
   import Cell
   import requests
   from SysdbHelperUtils import SysdbPathHelper
except ImportError as e:
   if sys.version_info < (3,) and os.path.exists( '/usr/bin/python3' ):
      os.execl( '/usr/bin/python3', 'python3', os.path.abspath(__file__ ) )
   else:
      log("Python3 not found. Attempting EOS version upgrade")
      tryImageUpgrade( e )

try:
   # This import will fail for EOS < 4.30.1, where #!/usr/bin/python
   # will run this in a python 2 environment
   from urllib.parse import urlparse
except ImportError:
   from urlparse import urlparse

class BootstrapManager( object ):
   def __init__( self ):
      super( BootstrapManager, self ).__init__()
      self.redirectorURL = None
      self.tokenType = None
      self.enrollAddr = None

   def getBootstrapURL( self, addr ):
      # urlparse in py3 parses correctly only if the url is properly introduced by //
      if not ( addr.startswith( '//' ) or addr.startswith( 'http://' ) or
               addr.startswith( 'https://' ) ):
         addr = '//' + addr
      if isinstance( self, CloudBootstrapManager ):
         addr = addr.replace( "apiserver", "www" )
      addrURL = urlparse( addr )
      if addrURL.netloc == "":
         addrURL = addrURL._replace( path="", netloc=addrURL.path )
      if addrURL.path == "":
         addrURL = addrURL._replace( path="/ztp/bootstrap" )
      if addrURL.scheme == "":
         if isinstance( self, CloudBootstrapManager ):
            addrURL = addrURL._replace( scheme="https" )
         else:
            addrURL = addrURL._replace( scheme="http" )
      return addrURL


##################################################################################
# step 1: get client certificate using the enrollment token
##################################################################################
   def getClientCertificates( self ):
      with open( TOKEN_FILE_PATH, "w" ) as f:
         f.write( enrollmentToken )

      # A timeout of 60 seconds is used with TerminAttr commands since in most
      # versions of TerminAttr, the command execution does not finish if a wrong
      # flag is specified leading to the catch block being never executed
      cmd = "timeout 60s "
      cmd += "/usr/bin/TerminAttr"
      cmd += " -cvauth " + self.tokenType + "," + TOKEN_FILE_PATH
      cmd += " -cvaddr " + self.enrollAddr
      cmd += " -enrollonly"

      # Use cvproxy only when it is specified, this is to ensure that if we are on
      # older version of EOS that doesn't support cvporxy flag, the script won't fail
      if cvproxy != "":
         cmd += " -cvproxy=" + cvproxy

      try:
         subprocess.check_output( cmd, shell=True, stderr=subprocess.STDOUT )
      except subprocess.CalledProcessError as e:
         # If the above subprocess call times out, it means that -cvproxy
         # flag is not present in the Terminattr version running on that device
         # Hence we have to do an image upgrade in this case.
         if e.returncode == 124: # timeout
            log("terminattr enrollment timed out, err - %s" % e.output)
            log("Attempting EOS version upgrade")
            tryImageUpgrade( e )
         else:
            log("Failed to retrieve certs, err - %s" % e.output )
            raise e

      log("step 1 done, exchanged enrollment token for client certificates")


##################################################################################
# Step 2: get the path of stored client certificate
##################################################################################
   def getCertificatePaths( self ):
      # Timeout added for TerminAttr
      cmd = "timeout 60s "
      cmd += "/usr/bin/TerminAttr"
      cmd += " -cvaddr " + self.enrollAddr
      cmd += " -certsconfig"

      try:
         response = subprocess.check_output( cmd, shell=True, stderr=subprocess.STDOUT,
         universal_newlines=True )
         json_response = json.loads( response )
         self.certificate = str( json_response[ self.enrollAddr ][ 'certFile' ] )
         self.key = str( json_response[ self.enrollAddr ][ 'keyFile' ] )
      except subprocess.CalledProcessError:
         log("Using fallback paths for client certs")
         basePath = "/persist/secure/ssl/terminattr/primary/"
         self.certificate = basePath + "certs/client.crt"
         self.key = basePath + "keys/client.key"

      log("step 2 done, obtained client certs location from TA" )
      log("certificate location - %s " % self.certificate )
      log("key location - %s " % self.key )


##################################################################################
# step 3 get bootstrap script using the certificates
##################################################################################
   def checkWithRedirector( self, serialNum ):
      if not self.redirectorURL:
         return

      try:
         payload = '{"key": {"system_id": "%s"}}' % serialNum
         response = requests.post( self.redirectorURL.geturl(), data=payload,
               cert=( self.certificate, self.key ), proxies=proxies )
         response.raise_for_status()
         clusters = response.json()[ 0 ][ "value" ][ "clusters" ][ "values" ]
         assignment = clusters [ 0 ][ "hosts" ][ "values" ][ 0 ]
         self.bootstrapURL = self.getBootstrapURL( assignment )
      except Exception as e:
         log("No assignment found. Error talking to redirector - %s" % e )

   def getBootstrapScript( self ):
      # setting Sysdb access variables
      sysname = os.environ.get( "SYSNAME", "ar" )
      pathHelper = SysdbPathHelper( sysname )

      # sysdb paths accessed
      cellID = str( Cell.cellId() )
      mibStatus = pathHelper.getEntity( "hardware/entmib" )
      tpmStatus = pathHelper.getEntity( "cell/" + cellID + "/hardware/tpm/status" )

      # setting header information
      headers = {}
      headers[ 'X-Arista-SystemMAC' ] = mibStatus.systemMacAddr
      headers[ 'X-Arista-ModelName' ] = mibStatus.root.modelName
      headers[ 'X-Arista-HardwareVersion' ] = mibStatus.root.hardwareRev
      headers[ 'X-Arista-Serial' ] = mibStatus.root.serialNum

      headers[ 'X-Arista-TpmApi' ] = tpmStatus.tpmVersion
      headers[ 'X-Arista-TpmFwVersion' ] = tpmStatus.firmwareVersion
      headers[ 'X-Arista-SecureZtp' ] = str( tpmStatus.boardValidated )

      headers[ 'X-Arista-SoftwareVersion' ] = getValueFromFile(
            "/etc/swi-version", "SWI_VERSION" )
      headers[ 'X-Arista-Architecture' ] = getValueFromFile( "/etc/arch", "" )
      headers[ 'X-Arista-CustomBootScriptVersion' ] = VERSION

      # get the URL to the right cluster
      self.checkWithRedirector( mibStatus.root.serialNum )

      # making the request and writing to file
      response = requests.get( self.bootstrapURL.geturl(), headers=headers,
            cert=( self.certificate, self.key ), proxies=proxies )
      response.raise_for_status()
      with open( BOOT_SCRIPT_PATH, "w" ) as f:
         f.write( response.text )

      log("step 3.1 done, bootstrap script fetched and stored at %s" % BOOT_SCRIPT_PATH)

   # execute the obtained bootstrap file
   def executeBootstrap( self ):
      proc = None
      def handleSigterm( sig, frame ):
         if proc is not None:
            proc.terminate()
         sys.exit( 127 + signal.SIGTERM )
      # The bootstrap script and challenge script anyway contain the required shebang for a
      # particualar EOS version, hence instead of re-evaluating here, we can easily just execute
      # it from that shebang itself.
      cmd = [ "chmod +x " + BOOT_SCRIPT_PATH ]
      try:
         subprocess.check_output( cmd, shell=True, stderr=subprocess.STDOUT )
      except subprocess.CalledProcessError as e:
         log(e.output)
         raise e
      log("step 3.2.1 done, execution permissions for bootstrap script setup")

      cmd = BOOT_SCRIPT_PATH
      os.environ['CVPROXY'] = cvproxy
      try:
         signal.signal( signal.SIGTERM, handleSigterm )
         proc = subprocess.Popen( [ cmd ], shell=True, stderr=subprocess.STDOUT,
                                  env=os.environ )
         proc.communicate()
         if proc.returncode:
            log("Bootstrap script failed with return code {}".format(proc.returncode))
            sys.exit( proc.returncode )
      except subprocess.CalledProcessError as e:
         log(e.output)
         raise e
      log("step 3.2.2 done, executed the fetched bootstrap script")

   def run( self ):
      self.getClientCertificates()
      self.getCertificatePaths()
      self.getBootstrapScript()
      self.executeBootstrap()


class CloudBootstrapManager( BootstrapManager ):
   def __init__( self ):
      super( CloudBootstrapManager, self ).__init__()

      self.bootstrapURL = self.getBootstrapURL( cvAddr )
      self.redirectorURL = self.bootstrapURL._replace( path=REDIRECTOR_PATH )
      self.tokenType = SECURE_TOKEN
      self.enrollAddr = self.bootstrapURL.netloc + ":" + SECURE_HTTPS_PORT
      self.enrollAddr = self.enrollAddr.replace( "www", "apiserver" )


class OnPremBootstrapManager( BootstrapManager ):
   def __init__( self ):
      super( OnPremBootstrapManager, self ).__init__()

      self.bootstrapURL = self.getBootstrapURL( cvAddr )
      self.redirectorURL = None
      self.tokenType = INGEST_TOKEN
      self.enrollAddr = self.bootstrapURL.netloc

if __name__ == "__main__":
   setupLogger()

   #logging the current version of the custom bootstrap script
   log( "Current Custom Bootstrap Script Version :%s" % VERSION )

   if cvAddr == "":
      err = "error: address to CVP missing"
      log(err)
      sys.exit(err)
   if enrollmentToken == "":
      err = "error: enrollment token missing"
      log(err)
      sys.exit(err)

   # restart ntp process in case a ntpServer value is passed.
   if ntpServer != "":
      configureAndRestartNTP(ntpServer)

   # check for enrollment token expiry
   expiryEpoch, parseSuccess = getExpiryFromToken(enrollmentToken)
   if parseSuccess and time.time() > expiryEpoch:
      expiry = datetime.datetime.fromtimestamp(expiryEpoch)
      err = "error: enrollment token expired. expired on: " + str(expiry) + " GMT"
      log(err)
      sys.exit(err)

   # check whether it is cloud or on prem
   if cvAddr.find( "arista.io" ) != -1 :
      bm = CloudBootstrapManager()
   else:
      bm = OnPremBootstrapManager()

   # run the script
   bm.run()
