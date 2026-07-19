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
//
// Per camera it folds distortion + cylindrical projection + rotation into one remap,
// then hard-seam composites the two halves.
//
// Uses only core/imgproc/imgcodecs/videoio, so it builds on both OpenCV 4.x and 5.x.
//
// Build:  cmake -S . -B build && cmake --build build
// Run:    ./build/StitchPipeline --source <image-or-video> [--calib-dir ../calibration]
//                                [--out DIR] [--start N] [--end N] [--degrees D] [--seam X]

#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/videoio.hpp>
#include <iostream>
#include <fstream>
#include <filesystem>
#include <vector>
#include <cmath>
#include <string>
#include <algorithm>
#include "json.hpp"

using json = nlohmann::json;
using namespace std;
using namespace cv;
namespace fs = std::filesystem;

// Precomputed, frame-independent stitch geometry.
struct StitchMaps
{
    Mat mapLx, mapLy, mapRx, mapRy;   // per-camera combined remap (undistort+cyl+rotation)
    int OW = 0, OH = 0;               // panorama size
    int seam = 0;                     // hard-seam column
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

// Forward Brown-Conrady: undistorted normalized (x,y) -> distorted (xd,yd).
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

// One combined remap: common-cylinder pixel -> source pixel in this camera's frame.
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
            double dx = sin(th), dy = hh, dz = cos(th);          // ray in LEFT frame
            double cxr = r00 * dx + r01 * dy + r02 * dz;         // rotate into camera
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

// Build the frame-independent maps + canvas + seam from calibration and frame size.
static StitchMaps buildStitchMaps(const Mat &KL, const vector<double> &DL,
                                  const Mat &KR, const vector<double> &DR,
                                  const Mat &R, int w, int h, int seamArg)
{
    StitchMaps m;
    double fcyl = KL.at<double>(0, 0);
    double halfL = atan(w / (2 * KL.at<double>(0, 0)));
    double halfR = atan(w / (2 * KR.at<double>(0, 0)));
    double yawR = atan2(R.at<double>(2, 0), R.at<double>(2, 2));   // right axis yaw in left frame
    double pad = 3.0 * CV_PI / 180.0;
    double thetaMin = min(-halfL, yawR - halfR) - pad;
    double thetaMax = max(halfL, yawR + halfR) + pad;
    double vhalf = atan(h / (2 * KL.at<double>(1, 1)));
    m.OW = min((int)((thetaMax - thetaMin) * fcyl), 12000);
    m.OH = min((int)(2 * tan(vhalf) * fcyl), 4000);
    vector<double> theta(m.OW), hval(m.OH);
    for (int i = 0; i < m.OW; i++) theta[i] = thetaMin + i / fcyl;
    for (int i = 0; i < m.OH; i++) hval[i] = (i - m.OH / 2.0) / fcyl;

    Mat okL, okR;
    buildCylMap(KL, DL, Mat::eye(3, 3, CV_64F), theta, hval, w, h, m.mapLx, m.mapLy, okL);
    buildCylMap(KR, DR, R, theta, hval, w, h, m.mapRx, m.mapRy, okR);

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
    cout << "panorama " << m.OW << "x" << m.OH
         << ", right yaw " << yawR * 180.0 / CV_PI << " deg, hard seam @ " << m.seam
         << (overlapCols.empty() ? "  [!! no overlap]" : "") << "\n";
    return m;
}

// Warp both halves of a combined frame onto the cylinder (before compositing).
static void warpHalves(const Mat &frame, const StitchMaps &m, Mat &warpL, Mat &warpR)
{
    int w = frame.cols / 2, h = frame.rows;
    Mat left = frame(Rect(0, 0, w, h));
    Mat right = frame(Rect(w, 0, frame.cols - w, h));
    remap(left, warpL, m.mapLx, m.mapLy, INTER_LINEAR, BORDER_CONSTANT);
    remap(right, warpR, m.mapRx, m.mapRy, INTER_LINEAR, BORDER_CONSTANT);
}

static Mat composite(const Mat &warpL, const Mat &warpR, const StitchMaps &m, double degrees)
{
    Mat pano = Mat::zeros(m.OH, m.OW, warpL.type());
    warpL(Rect(0, 0, m.seam, m.OH)).copyTo(pano(Rect(0, 0, m.seam, m.OH)));
    warpR(Rect(m.seam, 0, m.OW - m.seam, m.OH)).copyTo(pano(Rect(m.seam, 0, m.OW - m.seam, m.OH)));
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

static string argVal(int argc, char **argv, const string &key, const string &def)
{
    for (int i = 1; i < argc - 1; i++)
        if (key == argv[i]) return argv[i + 1];
    return def;
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
    // --source is the new name; --image kept as an alias for back-compat.
    string source = argVal(argc, argv, "--source", argVal(argc, argv, "--image", ""));
    string calibDir = argVal(argc, argv, "--calib-dir", "../calibration");
    string outDir = argVal(argc, argv, "--out", "pipeline_out_cpp");
    double degrees = stod(argVal(argc, argv, "--degrees", "0"));
    int seamArg = stoi(argVal(argc, argv, "--seam", "-1"));
    int startFrame = stoi(argVal(argc, argv, "--start", "0"));
    int endFrame = stoi(argVal(argc, argv, "--end", "-1"));   // -1 = last frame
    if (source.empty())
    {
        cerr << "Usage: StitchPipeline --source <image-or-video> [--calib-dir ..] [--out ..]\n"
             << "                      [--start N] [--end N] [--degrees D] [--seam X]\n";
        return 1;
    }
    fs::create_directories(outDir);

    Mat KL, KR, R;
    vector<double> DL, DR;
    loadIntrinsics(calibDir + "/left_intrinsics.json", KL, DL);
    loadIntrinsics(calibDir + "/right_intrinsics.json", KR, DR);
    R = loadRotation(calibDir + "/stereo_extrinsics.json");   // LEFT ray -> RIGHT ray

    // ---------- IMAGE: stitch a single frame ----------
    if (!isVideoFile(source))
    {
        Mat img = imread(source);
        if (img.empty()) { cerr << "Cannot read image: " << source << endl; return 1; }
        StitchMaps m = buildStitchMaps(KL, DL, KR, DR, R, img.cols / 2, img.rows, seamArg);
        Mat warpL, warpR;
        warpHalves(img, m, warpL, warpR);
        Mat pano = composite(warpL, warpR, m, degrees);
        Mat overlap;
        addWeighted(warpL, 0.5, warpR, 0.5, 0.0, overlap);
        imwrite(outDir + "/pano.jpg", pano);
        imwrite(outDir + "/overlap_5050.jpg", overlap);
        cout << "image -> " << outDir << "/pano.jpg (+ overlap_5050.jpg)\n";
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
    Mat frame, warpL, warpR;
    int written = 0;
    for (int i = startFrame; i <= endFrame; i++)
    {
        if (!cap.read(frame) || frame.empty()) break;
        warpHalves(frame, m, warpL, warpR);
        Mat pano = composite(warpL, warpR, m, degrees);
        writer.write(pano);
        if (i == startFrame)   // first-frame alignment debug
        {
            Mat overlap;
            addWeighted(warpL, 0.5, warpR, 0.5, 0.0, overlap);
            imwrite(outDir + "/overlap_5050.jpg", overlap);
        }
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
