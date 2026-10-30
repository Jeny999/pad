@echo off
rem One-time: allow the phone to reach the pad server on the home network.
netsh advfirewall firewall add rule name="Pad 8090" dir=in action=allow protocol=TCP localport=8090 profile=private
