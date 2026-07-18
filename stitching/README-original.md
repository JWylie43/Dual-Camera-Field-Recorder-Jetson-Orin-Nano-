# video-stitching

cmake -S . -B build && cmake --build build --config Debug
.\build\Debug\StitchingApplication.exe C:\Users\brown\Desktop\stitching_application\output\synced_left.mp4 C:\Users\brown\Desktop\stitching_application\output\synced_right.mp4
.\build\Debug\StitchingApplication.exe stitching_config.json
..\build\Debug\StitchingApplication.exe ..\stitching_config.json

ffmpeg -y -i stitched_video.mp4 -i output/left_audio.wav -c:v copy -c:a aac -shortest stitched_video_with_audio.mp4
