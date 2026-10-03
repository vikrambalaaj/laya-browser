"""laya-serve with limits sized for Jev Browser, which offers up to 200 page elements
as one choice question. Upstream caps a choice at 100 options (module constants, no
environment knob), so this launcher raises them in-process and then runs the normal CLI."""
import laya.serve as serve

serve.MAX_CHOICE_OPTIONS = 256
serve.MAX_TOTAL_OPTIONS = 1024

if __name__ == "__main__":
    serve.main()
