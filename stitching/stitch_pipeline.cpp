// stitch_pipeline.cpp - calibration-driven cylindrical stitch (C++), NO feature detection.
//
// C++ pipeline for the dual-camera rig. Reads the rig calibration (left/right
// intrinsics + stereo extrinsics) and stitches the combined LEFT|RIGHT feed into a
// cylindrical panorama, aligning the cameras purely from the extrinsic rotation R.
// There is no BRISK / matcher / findHomography anywhere.
//
// The remap maps depend only on the calibration, so they are built ONCE and reused:
//   * image source (.jpg/.png/...) -> stitch the single frame  -> pano.jpg
//   * video source (.mp4/.mkv/...)  -> loop frames [start..end] -> stitched_video.mp4
//   * --tune                        -> write an interactive tune.html (see below)
//
// --shift-x N nudges the RIGHT image N pixels horizontally (+ = right) before the
// hard-seam composite. Rotation-only alignment is exact only at infinity; a small
// shift lets you sharpen a chosen plane (e.g. a screen) at finite distance. Dial the
// value in visually with --tune, then pass it to a normal run.
//
// --tune writes a self-contained tune.html: it warps the first frame once, embeds the
// two cylinder halves, and lets you click arrows to shift the right image / move the
// seam and watch the stitch update live (arrow keys too). It prints the matching
// "--shift-x N --seam N" to use.
//
// GPU: per-frame work runs on cv::UMat (OpenCL when available, CPU fallback).
// Uses only core/imgproc/imgcodecs/videoio, so it builds on OpenCV 4.x and 5.x.
//
// Build:  cmake -S . -B build && cmake --build build
// Run:    ./build/StitchPipeline --source <image-or-video> [--calib-dir ../calibration]
//                     [--out DIR] [--start N] [--end N] [--degrees D] [--seam X]
//                     [--shift-x N] [--tune]

#include <opencv2/core.hpp>
#include <opencv2/core/ocl.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/videoio.hpp>
#include <iostream>
#include <fstream>
#include <filesystem>
#include <vector>
#include <cmath>
#include <string>
#include <sstream>
#include <algorithm>
#include "json.hpp"

using json = nlohmann::json;
using namespace std;
using namespace cv;
namespace fs = std::filesystem;

// Precomputed, frame-independent stitch geometry. Maps held as UMats (uploaded once).
struct StitchMaps
{
    UMat mapLx, mapLy, mapRx, mapRy;
    int OW = 0, OH = 0;
    int seam = 0;
};

static void loadIntrinsics(const string &path, Mat &K, vector<double> &D)
{
    ifstream f(path);
    if (!f.is_open()) { cerr << "Cannot open " << path << endl; exit(1); }
    json j; f >> j;
    K = Mat::eye(3, 3, CV_64F);
    for (int r = 0; r < 3; r++)
        for (int c = 0; c < 3; c++)
            K.at<double>(r, c) = j["camera_matrix"][r][c].get<double>();
    D.clear();
    for (auto &v : j["distortion_coefficients"])
        D.push_back(v.get<double>());
}

static Mat loadRotation(const string &path)
{
    ifstream f(path);
    if (!f.is_open()) { cerr << "Cannot open " << path << endl; exit(1); }
    json j; f >> j;
    Mat R(3, 3, CV_64F);
    for (int r = 0; r < 3; r++)
        for (int c = 0; c < 3; c++)
            R.at<double>(r, c) = j["rotation_matrix"][r][c].get<double>();
    return R;
}

static inline void applyDistortion(double x, double y, const vector<double> &D,
                                   double &xd, double &yd)
{
    double k1 = D.size() > 0 ? D[0] : 0, k2 = D.size() > 1 ? D[1] : 0;
    double p1 = D.size() > 2 ? D[2] : 0, p2 = D.size() > 3 ? D[3] : 0;
    double k3 = D.size() > 4 ? D[4] : 0, k4 = D.size() > 5 ? D[5] : 0;
    double k5 = D.size() > 6 ? D[6] : 0, k6 = D.size() > 7 ? D[7] : 0;
    double r2 = x * x + y * y;
    double radial = (1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2) /
                    (1 + k4 * r2 + k5 * r2 * r2 + k6 * r2 * r2 * r2);
    xd = x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x);
    yd = y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y;
}

static void buildCylMap(const Mat &K, const vector<double> &D, const Mat &R_cam_from_left,
                        const vector<double> &theta, const vector<double> &hval,
                        int w, int h, Mat &mapx, Mat &mapy, Mat &valid)
{
    int OW = (int)theta.size(), OH = (int)hval.size();
    mapx.create(OH, OW, CV_32F);
    mapy.create(OH, OW, CV_32F);
    valid = Mat::zeros(OH, OW, CV_8U);
    double fx = K.at<double>(0, 0), fy = K.at<double>(1, 1);
    double cx = K.at<double>(0, 2), cy = K.at<double>(1, 2);
    const Mat &R = R_cam_from_left;
    double r00 = R.at<double>(0, 0), r01 = R.at<double>(0, 1), r02 = R.at<double>(0, 2);
    double r10 = R.at<double>(1, 0), r11 = R.at<double>(1, 1), r12 = R.at<double>(1, 2);
    double r20 = R.at<double>(2, 0), r21 = R.at<double>(2, 1), r22 = R.at<double>(2, 2);

    for (int yy = 0; yy < OH; yy++)
    {
        double hh = hval[yy];
        float *mx = mapx.ptr<float>(yy);
        float *my = mapy.ptr<float>(yy);
        uchar *vv = valid.ptr<uchar>(yy);
        for (int xx = 0; xx < OW; xx++)
        {
            double th = theta[xx];
            double dx = sin(th), dy = hh, dz = cos(th);
            double cxr = r00 * dx + r01 * dy + r02 * dz;
            double cyr = r10 * dx + r11 * dy + r12 * dz;
            double czr = r20 * dx + r21 * dy + r22 * dz;
            if (czr <= 1e-6) { mx[xx] = my[xx] = -1.f; continue; }
            double xn = cxr / czr, yn = cyr / czr, xd, yd;
            applyDistortion(xn, yn, D, xd, yd);
            double u = fx * xd + cx, v = fy * yd + cy;
            if (u >= 0 && u < w && v >= 0 && v < h)
            { mx[xx] = (float)u; my[xx] = (float)v; vv[xx] = 1; }
            else { mx[xx] = my[xx] = -1.f; }
        }
    }
}

static StitchMaps buildStitchMaps(const Mat &KL, const vector<double> &DL,
                                  const Mat &KR, const vector<double> &DR,
                                  const Mat &R, int w, int h, int seamArg)
{
    StitchMaps m;
    double fcyl = KL.at<double>(0, 0);
    double halfL = atan(w / (2 * KL.at<double>(0, 0)));
    double halfR = atan(w / (2 * KR.at<double>(0, 0)));
    double yawR = atan2(R.at<double>(2, 0), R.at<double>(2, 2));
    double pad = 3.0 * CV_PI / 180.0;
    double thetaMin = min(-halfL, yawR - halfR) - pad;
    double thetaMax = max(halfL, yawR + halfR) + pad;
    double vhalf = atan(h / (2 * KL.at<double>(1, 1)));
    m.OW = min((int)((thetaMax - thetaMin) * fcyl), 12000);
    m.OH = min((int)(2 * tan(vhalf) * fcyl), 4000);
    vector<double> theta(m.OW), hval(m.OH);
    for (int i = 0; i < m.OW; i++) theta[i] = thetaMin + i / fcyl;
    for (int i = 0; i < m.OH; i++) hval[i] = (i - m.OH / 2.0) / fcyl;

    Mat mapLx, mapLy, mapRx, mapRy, okL, okR;
    buildCylMap(KL, DL, Mat::eye(3, 3, CV_64F), theta, hval, w, h, mapLx, mapLy, okL);
    buildCylMap(KR, DR, R, theta, hval, w, h, mapRx, mapRy, okR);

    vector<int> overlapCols;
    for (int x = 0; x < m.OW; x++)
    {
        bool lc = false, rc = false;
        for (int y = 0; y < m.OH && !(lc && rc); y++)
        { if (okL.at<uchar>(y, x)) lc = true; if (okR.at<uchar>(y, x)) rc = true; }
        if (lc && rc) overlapCols.push_back(x);
    }
    m.seam = seamArg >= 0 ? seamArg
             : (overlapCols.empty() ? m.OW / 2 : overlapCols[overlapCols.size() / 2]);

    mapLx.copyTo(m.mapLx); mapLy.copyTo(m.mapLy);
    mapRx.copyTo(m.mapRx); mapRy.copyTo(m.mapRy);

    cout << "panorama " << m.OW << "x" << m.OH
         << ", right yaw " << yawR * 180.0 / CV_PI << " deg, hard seam @ " << m.seam
         << (overlapCols.empty() ? "  [!! no overlap]" : "") << "\n";
    return m;
}

static void warpHalves(const UMat &frame, const StitchMaps &m, UMat &warpL, UMat &warpR)
{
    int w = frame.cols / 2, h = frame.rows;
    UMat left = frame(Rect(0, 0, w, h)).clone();
    UMat right = frame(Rect(w, 0, frame.cols - w, h)).clone();
    remap(left, warpL, m.mapLx, m.mapLy, INTER_LINEAR, BORDER_CONSTANT);
    remap(right, warpR, m.mapRx, m.mapRy, INTER_LINEAR, BORDER_CONSTANT);
}

// Hard-seam composite. shiftX nudges the right image horizontally (+ = right).
static UMat composite(const UMat &warpL, const UMat &warpR, const StitchMaps &m,
                      double degrees, double shiftX)
{
    UMat right = warpR;
    if (shiftX != 0.0)
    {
        Mat T = (Mat_<double>(2, 3) << 1, 0, shiftX, 0, 1, 0);
        warpAffine(warpR, right, T, Size(m.OW, m.OH));
    }
    UMat pano(m.OH, m.OW, warpL.type(), Scalar::all(0));
    warpL(Rect(0, 0, m.seam, m.OH)).copyTo(pano(Rect(0, 0, m.seam, m.OH)));
    right(Rect(m.seam, 0, m.OW - m.seam, m.OH)).copyTo(pano(Rect(m.seam, 0, m.OW - m.seam, m.OH)));
    if (degrees != 0.0)
    {
        double ang = degrees * CV_PI / 180.0;
        double a = cos(ang), b = sin(ang), ccx = m.OW / 2.0, ccy = m.OH / 2.0;
        Mat M = (Mat_<double>(2, 3) << a, b, (1 - a) * ccx - b * ccy,
                 -b, a, b * ccx + (1 - a) * ccy);
        warpAffine(pano, pano, M, Size(m.OW, m.OH));
    }
    return pano;
}

static string base64(const vector<uchar> &data)
{
    static const char *t = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    string out;
    int val = 0, bits = -6;
    for (uchar c : data)
    {
        val = (val << 8) + c;
        bits += 8;
        while (bits >= 0) { out.push_back(t[(val >> bits) & 0x3F]); bits -= 6; }
    }
    if (bits > -6) out.push_back(t[((val << 8) >> (bits + 8)) & 0x3F]);
    while (out.size() % 4) out.push_back('=');
    return out;
}

// Write a self-contained interactive tuning page for shift-x + seam.
static void writeTuneHtml(const UMat &warpL, const UMat &warpR, const StitchMaps &m,
                          const string &outDir)
{
    Mat mL, mR;
    warpL.copyTo(mL);
    warpR.copyTo(mR);
    vector<uchar> bufL, bufR;
    vector<int> q = {IMWRITE_JPEG_QUALITY, 85};
    imencode(".jpg", mL, bufL, q);
    imencode(".jpg", mR, bufR, q);

    ostringstream html;
    html << "<!doctype html><html><head><meta charset='utf-8'><title>Stitch tuner</title>"
         << "<script>const OW=" << m.OW << ",OH=" << m.OH << ",SEAM0=" << m.seam << ";"
         << "const IMGL='data:image/jpeg;base64," << base64(bufL) << "';"
         << "const IMGR='data:image/jpeg;base64," << base64(bufR) << "';</script>"
         << R"HTML(<style>
 body{margin:0;font-family:system-ui,sans-serif;background:#111;color:#eee}
 #bar{padding:10px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;
      background:#1b1b1b;position:sticky;top:0;z-index:2}
 button{font-size:16px;padding:6px 12px;border:0;border-radius:6px;background:#2a6f9e;color:#fff;cursor:pointer}
 .grp{display:flex;gap:8px;align-items:center;border:1px solid #333;padding:6px 10px;border-radius:8px}
 .val{min-width:46px;text-align:center;font-variant-numeric:tabular-nums;font-size:18px}
 #wrap{overflow:auto} canvas{display:block;max-width:100%;background:#000}
 code{background:#222;padding:3px 8px;border-radius:4px;user-select:all}
 .hint{color:#888;font-size:12px}
</style></head><body>
<div id="bar">
  <div class="grp">Shift-x <button id="sl">&#9664;</button><span class="val" id="sv">0</span><button id="sr">&#9654;</button></div>
  <div class="grp">Seam <button id="ml">&#9664;</button><span class="val" id="mv">0</span><button id="mr">&#9654;</button></div>
  <div class="grp"><label><input type="checkbox" id="blend"> overlap blend</label></div>
  <div class="grp">step <select id="step"><option>1</option><option>2</option><option>5</option><option>10</option></select></div>
  <div>run with: <code id="cmd"></code></div>
  <div class="hint">&#8592;/&#8594; shift &nbsp; [ / ] seam</div>
</div>
<div id="wrap"><canvas id="c"></canvas></div>
<script>
const cv=document.getElementById('c'), ctx=cv.getContext('2d');
cv.width=OW; cv.height=OH;
let shiftX=0, seam=SEAM0, loaded=0;
const imgL=new Image(), imgR=new Image();
imgL.onload=imgR.onload=()=>{ if(++loaded===2) render(); };
imgL.src=IMGL; imgR.src=IMGR;
const stepv=()=>parseInt(document.getElementById('step').value)||1;
function render(){
  ctx.clearRect(0,0,OW,OH);
  if(document.getElementById('blend').checked){
    ctx.globalAlpha=0.5; ctx.drawImage(imgL,0,0);
    ctx.drawImage(imgR,shiftX,0); ctx.globalAlpha=1;
  } else {
    ctx.drawImage(imgL,0,0);
    ctx.save(); ctx.beginPath(); ctx.rect(seam,0,OW-seam,OH); ctx.clip();
    ctx.drawImage(imgR,shiftX,0); ctx.restore();
  }
  ctx.strokeStyle='#f33'; ctx.lineWidth=2;
  ctx.beginPath(); ctx.moveTo(seam,0); ctx.lineTo(seam,OH); ctx.stroke();
  document.getElementById('sv').textContent=shiftX;
  document.getElementById('mv').textContent=seam;
  document.getElementById('cmd').textContent='--shift-x '+shiftX+' --seam '+seam;
}
const clampSeam=v=>Math.max(0,Math.min(OW,v));
document.getElementById('sl').onclick=()=>{shiftX-=stepv();render();};
document.getElementById('sr').onclick=()=>{shiftX+=stepv();render();};
document.getElementById('ml').onclick=()=>{seam=clampSeam(seam-stepv());render();};
document.getElementById('mr').onclick=()=>{seam=clampSeam(seam+stepv());render();};
document.getElementById('blend').onchange=render;
addEventListener('keydown',e=>{
  if(e.key==='ArrowLeft'){shiftX-=stepv();render();e.preventDefault();}
  else if(e.key==='ArrowRight'){shiftX+=stepv();render();e.preventDefault();}
  else if(e.key==='['){seam=clampSeam(seam-stepv());render();}
  else if(e.key===']'){seam=clampSeam(seam+stepv());render();}
});
</script></body></html>)HTML";

    string path = outDir + "/tune.html";
    ofstream f(path);
    f << html.str();
    f.close();
    cout << "tuner -> " << path << "  (open in a browser; dial in --shift-x / --seam)\n";
}

static string argVal(int argc, char **argv, const string &key, const string &def)
{
    for (int i = 1; i < argc - 1; i++)
        if (key == argv[i]) return argv[i + 1];
    return def;
}

static bool hasArg(int argc, char **argv, const string &key)
{
    for (int i = 1; i < argc; i++)
        if (key == argv[i]) return true;
    return false;
}

static bool isVideoFile(const string &path)
{
    string ext = fs::path(path).extension().string();
    transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
    static const vector<string> vids = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm"};
    return find(vids.begin(), vids.end(), ext) != vids.end();
}

int main(int argc, char **argv)
{
    string source = argVal(argc, argv, "--source", argVal(argc, argv, "--image", ""));
    string calibDir = argVal(argc, argv, "--calib-dir", "../calibration");
    string outDir = argVal(argc, argv, "--out", "pipeline_out");
    double degrees = stod(argVal(argc, argv, "--degrees", "0"));
    int seamArg = stoi(argVal(argc, argv, "--seam", "-1"));
    int startFrame = stoi(argVal(argc, argv, "--start", "0"));
    int endFrame = stoi(argVal(argc, argv, "--end", "-1"));
    double shiftX = stod(argVal(argc, argv, "--shift-x", "0"));
    bool tune = hasArg(argc, argv, "--tune");
    if (source.empty())
    {
        cerr << "Usage: StitchPipeline --source <image-or-video> [--calib-dir ..] [--out ..]\n"
             << "        [--start N] [--end N] [--degrees D] [--seam X] [--shift-x N] [--tune]\n";
        return 1;
    }
    fs::create_directories(outDir);

    ocl::setUseOpenCL(true);
    cout << "OpenCL available: " << ocl::haveOpenCL()
         << ", using GPU: " << ocl::useOpenCL() << "\n";

    Mat KL, KR, R;
    vector<double> DL, DR;
    loadIntrinsics(calibDir + "/left_intrinsics.json", KL, DL);
    loadIntrinsics(calibDir + "/right_intrinsics.json", KR, DR);
    R = loadRotation(calibDir + "/stereo_extrinsics.json");

    bool video = isVideoFile(source);

    // grab the first frame (single image, or first/--start frame of a video)
    auto firstFrame = [&](Mat &frame) -> bool {
        if (!video) { frame = imread(source); return !frame.empty(); }
        VideoCapture cap(source);
        if (!cap.isOpened()) return false;
        if (startFrame > 0) cap.set(CAP_PROP_POS_FRAMES, startFrame);
        bool ok = cap.read(frame);
        cap.release();
        return ok && !frame.empty();
    };

    // ---------- TUNE: warp one frame, write the interactive page ----------
    if (tune)
    {
        Mat frame;
        if (!firstFrame(frame)) { cerr << "Cannot read source: " << source << endl; return 1; }
        StitchMaps m = buildStitchMaps(KL, DL, KR, DR, R, frame.cols / 2, frame.rows, seamArg);
        UMat uFrame, warpL, warpR;
        frame.copyTo(uFrame);
        warpHalves(uFrame, m, warpL, warpR);
        writeTuneHtml(warpL, warpR, m, outDir);
        return 0;
    }

    // ---------- IMAGE: stitch a single frame ----------
    if (!video)
    {
        Mat img = imread(source);
        if (img.empty()) { cerr << "Cannot read image: " << source << endl; return 1; }
        StitchMaps m = buildStitchMaps(KL, DL, KR, DR, R, img.cols / 2, img.rows, seamArg);
        UMat uImg, warpL, warpR;
        img.copyTo(uImg);
        warpHalves(uImg, m, warpL, warpR);
        UMat uPano = composite(warpL, warpR, m, degrees, shiftX);
        Mat pano; uPano.copyTo(pano);
        imwrite(outDir + "/pano.jpg", pano);
        cout << "image -> " << outDir << "/pano.jpg\n";
        return 0;
    }

    // ---------- VIDEO: build maps once, loop frames [start..end] ----------
    VideoCapture cap(source);
    if (!cap.isOpened()) { cerr << "Cannot open video: " << source << endl; return 1; }
    int total = (int)cap.get(CAP_PROP_FRAME_COUNT);
    int fw = (int)cap.get(CAP_PROP_FRAME_WIDTH);
    int fh = (int)cap.get(CAP_PROP_FRAME_HEIGHT);
    double fps = cap.get(CAP_PROP_FPS);
    if (fps <= 0) fps = 30.0;
    if (endFrame < 0 || endFrame >= total) endFrame = total - 1;
    if (startFrame < 0) startFrame = 0;
    cout << "video: " << total << " frames @ " << fps << " fps, " << fw << "x" << fh
         << " -> frames [" << startFrame << ".." << endFrame << "]\n";

    StitchMaps m = buildStitchMaps(KL, DL, KR, DR, R, fw / 2, fh, seamArg);

    string outPath = outDir + "/stitched_video.mp4";
    VideoWriter writer(outPath, VideoWriter::fourcc('m', 'p', '4', 'v'), fps, Size(m.OW, m.OH));
    if (!writer.isOpened()) { cerr << "Cannot open output video for writing: " << outPath << endl; return 1; }

    cap.set(CAP_PROP_POS_FRAMES, startFrame);
    Mat frame, pano;
    UMat uFrame, warpL, warpR;
    int written = 0;
    for (int i = startFrame; i <= endFrame; i++)
    {
        if (!cap.read(frame) || frame.empty()) break;
        frame.copyTo(uFrame);
        warpHalves(uFrame, m, warpL, warpR);
        UMat uPano = composite(warpL, warpR, m, degrees, shiftX);
        uPano.copyTo(pano);
        writer.write(pano);
        written++;
        if (written % 30 == 0 || i == endFrame)
            cout << "  " << (int)(100.0 * (i - startFrame + 1) / (endFrame - startFrame + 1))
                 << "%  (frame " << i << ")\n";
    }
    cap.release();
    writer.release();
    cout << "video -> " << outPath << "  (" << written << " frames)\n";
    return 0;
}
