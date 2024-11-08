# forgetmenot

It is a toy that does something at the touch of a button, in this case a Raspberry Pi Pico plays a mini organ, a music box.

The toy has at least 1 sibling toy.  When started, it connects to an IRC channel. When the play button is pressed, the music box plays and passes this status on to the IRC channel. The sibling toys, which can be located anywhere in the world, ‘see’ the status and also play in response.  You know that someone is thinking of you at that moment :-)  

When starting for the first time, or if the previous WiFi is no longer available, the toy opens a ‘Pico’ hotspot. You connect to the hotspot and enter your WiFi access data. This data is used to connect to the Internet on subsequent starts.

Hardware:  
- Mini barrel organ  
- Pi Pico  
- L298N Motor Drive Controller Board  
- Motor 60 rpm or faster  
- ESP8266-01 Wi-Fi Module (or new Pi Pico W)
- DRV8833 1.5A 2-channel DC motor drive board
- AMS1117 3.3V power supply module
- Micro USB Buchse zu DIP Adapterplatine



In this Documentaion I use old Pi Picos without built in WiFi just because I have some of them left.  

Improvements:
- A Hiscore System, those who think of you more often will be rewarded by playing longer, for example.
- Play a beep when ready
- Implement a visial play using LED, for example
  

  
