from gpiozero import LED, Buzzer


class LocalAlerts:
    def __init__(self):
        self.green = LED(17)
        self.yellow = LED(27)
        self.red = LED(22)
        self.buzzer = Buzzer(18)

    def clear(self):
        self.green.off()
        self.yellow.off()
        self.red.off()
        self.buzzer.off()

    def close(self):
        self.clear()
        self.green.close()
        self.yellow.close()
        self.red.close()
        self.buzzer.close()

    def set_status(self, status):
        self.clear()

        if status == "GREEN":
            self.green.on()

        elif status == "YELLOW":
            self.yellow.on()

        elif status == "GRAY":
            self.yellow.blink(on_time=0.3, off_time=0.7, n=None, background=True) # type: ignore

        elif status == "RED":
            self.red.on()
            self.buzzer.beep(  # type: ignore[attr-defined]
                on_time=0.8,
                off_time=0.7,
                n=None,
                background=True
            ) 

        elif status == "ORANGE":
            self.yellow.blink(on_time=0.2, off_time=0.4) # type: ignore
            self.buzzer.beep( # type: ignore[attr-defined]
                on_time=0.05,
                off_time=1.0,
                n=None,
                background=True
            )

        elif status == "STABILIZING":
            self.green.blink(on_time=0.2, off_time=0.8) # type: ignore
            self.yellow.blink(on_time=0.2, off_time=0.8) # type: ignore
            self.red.blink(on_time=0.2, off_time=0.8) # type: ignore
