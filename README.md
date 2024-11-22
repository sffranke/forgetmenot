# forgetmenot

It is a toy that does something at the touch of a button, in this case a Raspberry Pi Pico plays a mini organ, a music box.

The toy has at least 1 sibling toy.  When started, it connects to an IRC channel. When the play button is pressed, the music box plays and passes this status on to the IRC channel. The sibling toys, which can be located anywhere in the world, recognizes the status and also play in response.  You know that someone is thinking of you at that moment :-)  

When starting for the first time, or if the previous WiFi is no longer available, the toy opens a ‘Pico’ hotspot. You connect to the hotspot 192.168.4.1 and enter your WiFi access data. This data is used to connect to the Internet on subsequent starts.

Hardware:
- Mini barrel organ https://amzn.to/40GhTKe
- motor shaft connector https://amzn.to/3UFKeN1
- Motor 60 rpm or faster https://amzn.to/40Dn0e2
- or Mini barrel organ with motor (difficult to find)
- Pi Pico  https://amzn.to/3ClQl2D
- or Pi Pico W https://amzn.to/3YNYEfo
- ESP8266-01 Wi-Fi Module (not necessary if using a Pi Pico W) https://amzn.to/3UIIwKT
- DRV8833 1.5A 2-channel DC motor drive board https://amzn.to/40EvkKF
- AMS1117 3.3V power supply module https://amzn.to/4hHLWra
- Micro USB Buchse zu DIP Adapterplatine https://amzn.to/3YXfN7t

*Note: The links are affiliate links. If you make a purchase, I will receive a small commission without the price changing for you.*

In this Documentaion I use old Pi Picos without built in WiFi just because I have some of them left.  
The code is for the Pi Pico without built in WiFi, it needs some changes if you use the Pi Pico with built in WiFi (Pico W)!

<img src="forgetmenot_Steckplatine.png" width="400">

Possible future improvements:
- A Hiscore System, those who think of you more often will be rewarded by playing longer, for example.
- Play a beep when ready
- Implement a visial play using LED, for example
  
