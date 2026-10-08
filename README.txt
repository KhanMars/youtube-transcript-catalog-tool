##Prereq:##
pip install -r requirements.txt

##Run Transcript process:##
##Substitute the Youtube URL##
python pipeline.py --channel "https://www.youtube.com/@someuser" --data data --limit 3

or

python pipeline.py --channel "https://www.youtube.com/@someuser" --data data --limit 50 --cookies-from-browser chrome --sleep 4 --mode captions

##Run Transcript viewer, view in browser at http://127.0.0.1:7860##
python app.py --data data
