// stitch_pipeline.cpp - calibration-driven cylindrical stitch (C++), NO feature detection.
//
// Reads the rig calibration (left/right intrinsics + stereo extrinsics) and stitches
// the combined LEFT|RIGHT feed into a cylindrical panorama, aligning the cameras from
// the extrinsic rotation R. No BRISK / matcher / findHomography anywhere.
//
// Modes:
//   image source (.jpg/.png/...) -> stitch the single frame  -> pano.jpg
//   video source (.mp4/.mkv/...)  -> loop frames [start..end] -> stitched_video.mp4
//   --tune  -> launch an interactive browser tuner (see below)
//
// Right-image alignment (applied as one affine before the hard-seam composite):
//   --shift-top N     horizontal shift of the TOP rows   (aligns the FAR edge)
//   --shift-bottom N  horizontal shift of the BOTTOM rows (aligns the NEAR edge)
//   --shift-y N       vertical shift of the whole image
// If top != bottom this is a vertical SHEAR: the per-row horizontal shift is
// interpolated between the two, so a receding field (near at the bottom, far at the
// top) lines up along a straight vertical seam. (--shift-x N sets top=bottom=N.)
//
// --tune warps the first frame once, starts a localhost web server, opens a browser
// to a live tuner where you adjust those values and click "Stitch all frames" to run
// the full stitch (progress bar + done). One command; UI opens itself.
//
// GPU: per-frame work runs on cv::UMat (OpenCL when available, CPU fallback).
// Uses core/imgproc/imgcodecs/videoio; builds on OpenCV 4.x and 5.x.
//
// Build:  cmake -S . -B build && cmake --build build

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
#include <cstdlib>
#include <cstdio>
#include <algorithm>
#include <thread>
#include <atomic>
#include <mutex>
#include "json.hpp"

#ifdef _WIN32
  #include <winsock2.h>
  #include <ws2tcpip.h>
  using socket_t = SOCKET;
  #define CLOSESOCK closesocket
#else
  #include <sys/socket.h>
  #include <netinet/in.h>
  #include <arpa/inet.h>
  #include <unistd.h>
  using socket_t = int;
  #define CLOSESOCK close
  #define INVALID_SOCKET (-1)
#endif

#ifdef _WIN32
  #define popen _popen
  #define pclose _pclose
#endif

using json = nlohmann::json;
using namespace std;
using namespace cv;
namespace fs = std::filesystem;

struct StitchMaps
{
    UMat mapLx, mapLy, mapRx, mapRy;
    int OW = 0, OH = 0;
    int seam = 0;
    int ox0 = 0, ox1 = 0;   // overlap column range [ox0, ox1) where both cameras are valid
};

// Right-image alignment (per-row horizontal shear + vertical shift) + seam blend.
struct Align
{
    double shiftTop = 0, shiftBottom = 0, shiftY = 0;
    int bands = 0;       // multi-band (Laplacian) blend levels; 0 = hard seam
    bool exposure = false; // match right image brightness/color to left (per channel)
    bool smartSeam = false; // route the seam around moving objects (min-difference path)
};

// Shared progress state for the --tune server (stitch runs on a worker thread).
static std::atomic<int> g_percent{0};
static std::atomic<bool> g_busy{false};
static std::atomic<bool> g_done{false};
static std::mutex g_mu;
static string g_result;

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
    m.ox0 = overlapCols.empty() ? 0 : overlapCols.front();
    m.ox1 = overlapCols.empty() ? m.OW : overlapCols.back() + 1;

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

// Multi-band (Laplacian pyramid) blend of A (left) and B (right) across a sharp seam
// mask. Low frequencies blend over a wide band (smooth tone) and high frequencies over
// a narrow band (edges stay sharp) - no ghosting/blur, unlike a linear feather.
static UMat straightMask(int seam, int OW, int OH)
{
    Mat m = Mat::zeros(OH, OW, CV_32F);
    int s = max(0, min(seam, OW));
    if (s > 0) m(Rect(0, 0, s, OH)).setTo(1.0f);       // 1 = keep left, 0 = keep right
    UMat u; m.copyTo(u); return u;
}

// Dynamic seam: min-cost vertical path through the KNOWN overlap [ox0,ox1) so the cut
// weaves AROUND moving objects. Cost = image difference + a center bias (stay near the
// overlap centre) + a temporal term (stick to the previous frame's seam) so wind/noise
// doesn't make the seam jitter frame-to-frame - it only moves when a player forces it.
static UMat computeSeamMask(const UMat &warpL, const UMat &right, int ox0, int ox1,
                            int OW, int OH, vector<int> &prevSeam)
{
    int x0 = max(0, ox0), x1 = min(OW, ox1), bw = x1 - x0;
    if (bw < 4) { prevSeam.assign(OH, (x0 + x1) / 2); return straightMask((x0 + x1) / 2, OW, OH); }
    const double center = (x0 + x1) / 2.0;
    const float CB = 0.08f;   // center bias
    const float TW = 0.8f;    // temporal stickiness
    bool temporal = ((int)prevSeam.size() == OH);

    UMat lband = warpL(Rect(x0, 0, bw, OH)), rband = right(Rect(x0, 0, bw, OH));
    Mat L, R; lband.copyTo(L); rband.copyTo(R);       // download only the overlap band
    Mat gL, gR; cvtColor(L, gL, COLOR_BGR2GRAY); cvtColor(R, gR, COLOR_BGR2GRAY);
    Mat cost; absdiff(gL, gR, cost); cost.convertTo(cost, CV_32F);
    Mat bad = (gL < 5) | (gR < 5); cost.setTo(1e6f, bad);   // keep seam inside valid overlap
    for (int y = 0; y < OH; y++)
    {
        float *cp = cost.ptr<float>(y);
        for (int x = 0; x < bw; x++)
        {
            float gx = (float)(x0 + x);
            cp[x] += CB * fabsf(gx - (float)center);
            if (temporal) cp[x] += TW * fabsf(gx - (float)prevSeam[y]);
        }
    }
    Mat M = cost.clone(), back(OH, bw, CV_32S);
    for (int y = 1; y < OH; y++)
    {
        const float *mp = M.ptr<float>(y - 1);
        float *mc = M.ptr<float>(y);
        int *bk = back.ptr<int>(y);
        for (int x = 0; x < bw; x++)
        {
            float best = mp[x]; int bx = x;
            if (x > 0 && mp[x - 1] < best) { best = mp[x - 1]; bx = x - 1; }
            if (x < bw - 1 && mp[x + 1] < best) { best = mp[x + 1]; bx = x + 1; }
            mc[x] += best; bk[x] = bx;
        }
    }
    int cur = 0;
    { const float *last = M.ptr<float>(OH - 1); for (int x = 1; x < bw; x++) if (last[x] < last[cur]) cur = x; }
    prevSeam.assign(OH, 0);
    Mat mask = Mat::zeros(OH, OW, CV_32F);
    for (int y = OH - 1; y >= 0; y--)
    {
        int px = x0 + cur;
        prevSeam[y] = px;
        if (px > 0) mask(Rect(0, y, px, 1)).setTo(1.0f);
        if (y > 0) cur = back.ptr<int>(y)[cur];
    }
    UMat u; mask.copyTo(u); return u;
}

static UMat multiBandBlend(const UMat &A8, const UMat &B8, const UMat &maskF, int bands)
{
    bands = max(2, min(bands, 8));
    UMat A, B;
    A8.convertTo(A, CV_32FC3);
    B8.convertTo(B, CV_32FC3);
    vector<UMat> gA{A}, gB{B}, gM{maskF};
    for (int i = 1; i < bands; i++)
    {
        UMat da, db, dm;
        pyrDown(gA[i - 1], da); pyrDown(gB[i - 1], db); pyrDown(gM[i - 1], dm);
        gA.push_back(da); gB.push_back(db); gM.push_back(dm);
    }
    auto blendLevel = [](const UMat &la, const UMat &lb, const UMat &m1) {
        UMat m3, om, a_, b_, out;
        cvtColor(m1, m3, COLOR_GRAY2BGR);
        subtract(Scalar::all(1), m3, om);
        multiply(la, m3, a_); multiply(lb, om, b_);
        add(a_, b_, out);
        return out;
    };
    vector<UMat> ls(bands);
    ls[bands - 1] = blendLevel(gA[bands - 1], gB[bands - 1], gM[bands - 1]);
    for (int i = bands - 2; i >= 0; i--)
    {
        UMat ua, ub, la, lb;
        pyrUp(gA[i + 1], ua, gA[i].size()); subtract(gA[i], ua, la);
        pyrUp(gB[i + 1], ub, gB[i].size()); subtract(gB[i], ub, lb);
        ls[i] = blendLevel(la, lb, gM[i]);
    }
    UMat res = ls[bands - 1];
    for (int i = bands - 2; i >= 0; i--) { UMat up; pyrUp(res, up, ls[i].size()); add(up, ls[i], res); }
    UMat out; res.convertTo(out, CV_8UC3);
    return out;
}

// Per-channel gain so the right image's brightness/color matches the left. Gains are
// measured from the overlap band around the seam, then applied to the WHOLE right image.
static void exposureMatch(const UMat &warpL, UMat &right, int seam, int OW, int OH)
{
    int W = min(200, OW / 8);
    int x0 = max(0, seam - W), x1 = min(OW, seam + W);
    if (x1 - x0 < 2) return;
    Rect band(x0, 0, x1 - x0, OH);
    UMat gl, gr, mL8, mR8, mask;
    cvtColor(warpL(band), gl, COLOR_BGR2GRAY);
    cvtColor(right(band), gr, COLOR_BGR2GRAY);
    threshold(gl, mL8, 5, 255, THRESH_BINARY);
    threshold(gr, mR8, 5, 255, THRESH_BINARY);
    bitwise_and(mL8, mR8, mask);               // valid in BOTH (skip black wedges)
    if (countNonZero(mask) < 100) return;
    Scalar meanL = mean(warpL(band), mask);
    Scalar meanR = mean(right(band), mask);
    vector<UMat> ch; split(right, ch);
    for (int c = 0; c < 3; c++)
    {
        double g = meanR[c] > 1e-3 ? meanL[c] / meanR[c] : 1.0;
        g = max(0.3, min(3.0, g));             // clamp to avoid extreme corrections
        ch[c].convertTo(ch[c], CV_8U, g);      // saturating per-channel scale
    }
    merge(ch, right);
}

static UMat composite(const UMat &warpL, const UMat &warpR, const StitchMaps &m,
                      double degrees, const Align &a, vector<int> *prevSeam = nullptr)
{
    UMat right = warpR;
    if (a.shiftTop != 0.0 || a.shiftBottom != 0.0 || a.shiftY != 0.0)
    {
        // per-row horizontal shear (top->bottom) + vertical shift, as one affine.
        double k = (m.OH > 1) ? (a.shiftBottom - a.shiftTop) / (m.OH - 1) : 0.0;
        Mat T = (Mat_<double>(2, 3) << 1, k, a.shiftTop, 0, 1, a.shiftY);
        warpAffine(warpR, right, T, Size(m.OW, m.OH));
    }
    if (a.exposure) exposureMatch(warpL, right, m.seam, m.OW, m.OH);
    UMat mask;
    if (a.smartSeam)
    {
        vector<int> local;
        vector<int> &ps = prevSeam ? *prevSeam : local;   // temporal only within a video loop
        mask = computeSeamMask(warpL, right, m.ox0, m.ox1, m.OW, m.OH, ps);
    }
    else
        mask = straightMask(m.seam, m.OW, m.OH);
    UMat pano;
    if (a.bands > 0)
    {
        pano = multiBandBlend(warpL, right, mask, a.bands);
    }
    else
    {
        UMat mask8; mask.convertTo(mask8, CV_8U, 255.0);
        pano = right.clone();
        warpL.copyTo(pano, mask8);          // left where mask, right elsewhere
    }
    if (degrees != 0.0)
    {
        double ang = degrees * CV_PI / 180.0;
        double c = cos(ang), s = sin(ang), ccx = m.OW / 2.0, ccy = m.OH / 2.0;
        Mat M = (Mat_<double>(2, 3) << c, s, (1 - c) * ccx - s * ccy,
                 -s, c, s * ccx + (1 - c) * ccy);
        warpAffine(pano, pano, M, Size(m.OW, m.OH));
    }
    return pano;
}

// POS_FRAMES seeking is unreliable on un-indexed MJPEG-MKV, so step sequentially:
// grab n frames (0..n-1) so the NEXT read() returns frame n.
static bool seekFrame(VideoCapture &cap, int n)
{
    for (int i = 0; i < n; i++)
        if (!cap.grab()) return false;
    return true;
}

static string stitchImageFile(const string &source, StitchMaps &m, double degrees,
                              const Align &a, const string &outDir)
{
    Mat img = imread(source);
    if (img.empty()) return "ERROR: cannot read image";
    UMat uImg, wL, wR;
    img.copyTo(uImg);
    warpHalves(uImg, m, wL, wR);
    UMat uPano = composite(wL, wR, m, degrees, a);
    Mat pano; uPano.copyTo(pano);
    string out = outDir + "/pano.jpg";
    imwrite(out, pano);
    return out;
}

static string stitchVideoFile(const string &source, StitchMaps &m, double degrees,
                              const Align &a, int startFrame, int endFrame, int totalFrames,
                              const string &outDir, std::atomic<int> *prog = nullptr)
{
    VideoCapture cap(source);
    if (!cap.isOpened()) return "ERROR: cannot open video";
    double fps = cap.get(CAP_PROP_FPS);
    if (fps <= 0) fps = 30.0;
    const int BIG = 1 << 30;
    int s = startFrame < 0 ? 0 : startFrame;
    int e = endFrame >= 0 ? endFrame : (totalFrames > 0 ? totalFrames - 1 : BIG);
    bool bounded = (e < BIG);
    string out = outDir + "/stitched_video.mp4";
    VideoWriter writer(out, VideoWriter::fourcc('m', 'p', '4', 'v'), fps, Size(m.OW, m.OH));
    if (!writer.isOpened()) return "ERROR: cannot open output video";
    seekFrame(cap, s);
    Mat frame, pano;
    UMat uFrame, wL, wR;
    vector<int> prevSeam;   // carried across frames for a temporally stable smart seam
    int written = 0;
    for (int i = s; i <= e; i++)
    {
        if (!cap.read(frame) || frame.empty()) break;   // also stops at EOF
        frame.copyTo(uFrame);
        warpHalves(uFrame, m, wL, wR);
        composite(wL, wR, m, degrees, a, &prevSeam).copyTo(pano);
        writer.write(pano);
        ++written;
        if (bounded)
        {
            int pct = (int)(100.0 * (i - s + 1) / (e - s + 1));
            if (prog) prog->store(pct);
            if (written % 30 == 0 || i == e) cout << "  " << pct << "%  (frame " << i << ")\n";
        }
        else if (written % 30 == 0) cout << "  frame " << i << "\n";
    }
    cap.release();
    writer.release();
    return out + "  (" + to_string(written) + " frames)";
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

static string tunerHtml(const UMat &warpL, const UMat &warpR, const StitchMaps &m,
                        int total, int startIdx, bool video)
{
    Mat mL, mR;
    warpL.copyTo(mL); warpR.copyTo(mR);
    vector<uchar> bL, bR; vector<int> q = {IMWRITE_JPEG_QUALITY, 85};
    imencode(".jpg", mL, bL, q);
    imencode(".jpg", mR, bR, q);
    ostringstream h;
    h << "<!doctype html><html><head><meta charset='utf-8'><title>Stitch tuner</title>"
      << "<script>const OW=" << m.OW << ",OH=" << m.OH << ",SEAM0=" << m.seam
      << ",TOTAL=" << total << ",FRAME0=" << startIdx
      << ",VIDEO=" << (video ? "true" : "false") << ";"
      << "const IMGL='data:image/jpeg;base64," << base64(bL) << "';"
      << "const IMGR='data:image/jpeg;base64," << base64(bR) << "';</script>"
      << R"HTML(<style>
 body{margin:0;font-family:system-ui,sans-serif;background:#111;color:#eee}
 #bar{padding:10px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:#1b1b1b;position:sticky;top:0;z-index:2}
 button{font-size:15px;padding:5px 10px;border:0;border-radius:6px;background:#2a6f9e;color:#fff;cursor:pointer}
 #stitch{background:#1f8a3b;font-weight:700} #quit{background:#8a3b1f} #finish{background:#555}
 .grp{display:flex;gap:6px;align-items:center;border:1px solid #333;padding:5px 9px;border-radius:8px;font-size:14px}
 .val{width:60px;text-align:center;font-variant-numeric:tabular-nums;font-size:15px;background:#222;color:#eee;border:1px solid #444;border-radius:5px;padding:3px}
 #wrap{overflow:auto} canvas{display:block;max-width:100%;background:#000}
 #status{padding:6px 10px;color:#9cf} .hint{color:#888;font-size:12px}
</style></head><body>
<div id="bar">
  <div class="grp">Shift far (top) <button id="tl">&#9664;</button><input class="val" id="tv" type="number" value="0"><button id="tr">&#9654;</button></div>
  <div class="grp">Shift near (bottom) <button id="bl">&#9664;</button><input class="val" id="bv" type="number" value="0"><button id="br">&#9654;</button></div>
  <div class="grp">Shift-y <button id="yl">&#9664;</button><input class="val" id="yv" type="number" value="0"><button id="yr">&#9654;</button></div>
  <div class="grp">Seam <button id="ml">&#9664;</button><input class="val" id="mv" type="number" value="0"><button id="mr">&#9654;</button></div>
  <div class="grp"><label><input type="checkbox" id="mb" checked> multi-band blend</label></div>
  <div class="grp"><label><input type="checkbox" id="xc"> exposure/color match</label></div>
  <div class="grp"><label><input type="checkbox" id="ss"> seam avoidance (moving objects)</label></div>
  <div class="grp" id="framegrp">Frame <button id="fprev">&#9664;</button><input type="range" id="frange" min="0" value="0" style="vertical-align:middle;width:140px"><input class="val" id="fval" type="number" value="0"><span id="ftot" style="color:#9cf">/ ?</span><button id="fnext">&#9654;</button></div>
  <div class="grp"><label><input type="checkbox" id="blend"> overlap blend</label></div>
  <div class="grp">step <select id="step"><option>1</option><option>2</option><option>5</option><option>10</option></select></div>
  <button id="stitch">Stitch all frames</button>
  <button id="quit">Quit</button>
  <span class="hint">&#8592;/&#8594; shift both &nbsp; [ / ] seam</span>
</div>
<div id="status">Ready. Align the far (top) and near (bottom) edges, then Stitch.</div>
<div id="prog" style="padding:0 10px 10px;display:none">
  <progress id="pb" max="100" value="0" style="width:280px;height:16px;vertical-align:middle"></progress>
  <span id="pct" style="margin-left:8px">0%</span>
  <button id="finish" style="display:none;margin-left:12px">Finish &amp; stop</button>
</div>
<div id="wrap"><canvas id="c"></canvas></div>
<script>
const cv=document.getElementById('c'), ctx=cv.getContext('2d');
cv.width=OW; cv.height=OH;
const clampSeam=v=>Math.max(0,Math.min(OW,v));
const stepv=()=>parseInt(document.getElementById('step').value)||1;
const st=t=>document.getElementById('status').textContent=t;
// number inputs are the source of truth
const tv=document.getElementById('tv'), bv=document.getElementById('bv'),
      yv=document.getElementById('yv'), mv=document.getElementById('mv');
mv.value=SEAM0;
let sTop=0, sBot=0, sY=0, seam=SEAM0, pending=0;
const imgL=new Image(), imgR=new Image();
function both(){ if(--pending<=0){ pending=0; render(); } }
imgL.onload=imgR.onload=both;
function drawRight(){
  const k=(OH>1)?(sBot-sTop)/(OH-1):0;         // per-row shear slope
  ctx.save(); ctx.transform(1,0,k,1,sTop,sY); ctx.drawImage(imgR,0,0); ctx.restore();
}
function render(){
  sTop=+tv.value||0; sBot=+bv.value||0; sY=+yv.value||0; seam=clampSeam(+mv.value||0);
  ctx.setTransform(1,0,0,1,0,0); ctx.globalAlpha=1; ctx.clearRect(0,0,OW,OH);
  if(document.getElementById('blend').checked){
    ctx.globalAlpha=0.5; ctx.drawImage(imgL,0,0); drawRight(); ctx.globalAlpha=1;
  } else {
    ctx.drawImage(imgL,0,0);
    ctx.save(); ctx.beginPath(); ctx.rect(seam,0,OW-seam,OH); ctx.clip(); drawRight(); ctx.restore();
  }
  ctx.strokeStyle='#f33'; ctx.lineWidth=2;
  ctx.beginPath(); ctx.moveTo(seam,0); ctx.lineTo(seam,OH); ctx.stroke();
}
const nudge=(el,d)=>{ el.value=(+el.value||0)+d; render(); };
tl.onclick=()=>nudge(tv,-stepv()); tr.onclick=()=>nudge(tv,stepv());
bl.onclick=()=>nudge(bv,-stepv()); br.onclick=()=>nudge(bv,stepv());
yl.onclick=()=>nudge(yv,-stepv()); yr.onclick=()=>nudge(yv,stepv());
ml.onclick=()=>nudge(mv,-stepv()); mr.onclick=()=>nudge(mv,stepv());
[tv,bv,yv,mv].forEach(el=>el.oninput=render);
document.getElementById('blend').onchange=render;
addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT') return;      // let typing in the boxes work normally
  const d=stepv();
  if(e.key==='ArrowLeft'){tv.value=(+tv.value||0)-d; bv.value=(+bv.value||0)-d; render(); e.preventDefault();}
  else if(e.key==='ArrowRight'){tv.value=(+tv.value||0)+d; bv.value=(+bv.value||0)+d; render(); e.preventDefault();}
  else if(e.key==='['){mv.value=clampSeam((+mv.value||0)-d); render();}
  else if(e.key===']'){mv.value=clampSeam((+mv.value||0)+d); render();}
});
pending=2; imgL.src=IMGL; imgR.src=IMGR;   // initial frame
// frame scrubbing (video only)
const frange=document.getElementById('frange'), fval=document.getElementById('fval');
const known = TOTAL>1;
const FMAX = known ? TOTAL-1 : 100000;
frange.max=FMAX; frange.value=FRAME0; fval.value=FRAME0; fval.max=FMAX;
document.getElementById('ftot').textContent = known ? ('/ '+TOTAL) : '/ ?';
if(!VIDEO) document.getElementById('framegrp').style.display='none';
const ftxt=n=>'Frame '+n+(known?(' / '+TOTAL):'');
function loadFrame(n){
  n=Math.max(0,Math.min(FMAX,parseInt(n)||0)); frange.value=n; fval.value=n;
  st('Loading '+ftxt(n)+'…');
  fetch('/frame?n='+n).then(r=>r.json()).then(d=>{
    if(d.error){ st('frame error: '+d.error); return; }
    pending=2; imgL.src=d.left; imgR.src=d.right; st(ftxt(n));
  }).catch(e=>st('frame load error: '+e));
}
document.getElementById('fprev').onclick=()=>loadFrame((+frange.value||0)-1);
document.getElementById('fnext').onclick=()=>loadFrame((+frange.value||0)+1);
frange.onchange=()=>loadFrame(frange.value);
fval.onchange=()=>loadFrame(fval.value);
let polling=null;
const pb=document.getElementById('pb'), pct=document.getElementById('pct');
const params=()=>'shifttop='+(+tv.value||0)+'&shiftbottom='+(+bv.value||0)+'&shifty='+(+yv.value||0)+'&seam='+clampSeam(+mv.value||0)+'&bands='+(document.getElementById('mb').checked?6:0)+'&exposure='+(document.getElementById('xc').checked?1:0)+'&smartseam='+(document.getElementById('ss').checked?1:0);
document.getElementById('stitch').onclick=async()=>{
  if(polling) return;
  document.getElementById('stitch').disabled=true;
  document.getElementById('prog').style.display='block';
  document.getElementById('finish').style.display='none';
  pb.value=0; pct.textContent='0%';
  st('Stitching all frames ('+params()+') …');
  try{ const r=await fetch('/stitch?'+params());
       if((await r.text())==='busy'){ st('Already stitching…'); return; } }
  catch(e){ st('Error starting: '+e); document.getElementById('stitch').disabled=false; return; }
  polling=setInterval(async()=>{
    try{
      const p=await (await fetch('/progress')).json();
      pb.value=p.percent; pct.textContent=p.percent+'%';
      if(p.done){
        clearInterval(polling); polling=null;
        document.getElementById('stitch').disabled=false;
        pb.value=100; pct.textContent='100%';
        st('✅ Done — saved to '+p.result);
        document.getElementById('finish').style.display='inline-block';
      }
    }catch(e){}
  },400);
};
document.getElementById('finish').onclick=async()=>{
  try{await fetch('/quit');}catch(e){}
  st('Finished — server stopped. You can close this tab.'); try{window.close();}catch(e){}
};
document.getElementById('quit').onclick=async()=>{ try{await fetch('/quit');}catch(e){} st('Stopped. You can close this tab.'); };
</script></body></html>)HTML";
    return h.str();
}

static string qparam(const string &query, const string &key)
{
    string k = key + "=";
    size_t p = query.find(k);
    if (p == string::npos) return "";
    size_t s = p + k.size(), e = query.find('&', s);
    return query.substr(s, e == string::npos ? string::npos : e - s);
}

static void openBrowser(const string &url)
{
#ifdef _WIN32
    system(("start \"\" \"" + url + "\"").c_str());
#elif __APPLE__
    system(("open \"" + url + "\"").c_str());
#else
    system(("xdg-open \"" + url + "\" >/dev/null 2>&1 &").c_str());
#endif
}

static void runTuneServer(const string &source, bool video, StitchMaps &m,
                          const UMat &warpL, const UMat &warpR, double degrees,
                          int startFrame, int endFrame, const string &outDir,
                          int totalFrames, int port)
{
#ifdef _WIN32
    WSADATA wsa; WSAStartup(MAKEWORD(2, 2), &wsa);
#endif
    string html = tunerHtml(warpL, warpR, m, totalFrames, startFrame, video);

    socket_t srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv == INVALID_SOCKET) { cerr << "socket() failed\n"; return; }
    int yes = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, (const char *)&yes, sizeof(yes));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    int bound = -1;
    for (int p = port; p < port + 10; ++p)
    {
        addr.sin_port = htons((unsigned short)p);
        if (::bind(srv, (sockaddr *)&addr, sizeof(addr)) == 0) { bound = p; break; }
    }
    if (bound < 0 || listen(srv, 8) != 0) { cerr << "Could not bind a port\n"; CLOSESOCK(srv); return; }

    string url = "http://127.0.0.1:" + to_string(bound) + "/";
    cout << "\nTuner running at " << url << "  (opening browser; Ctrl+C or Quit to stop)\n";
    openBrowser(url);

    VideoCapture frameCap;   // persistent for /frame scrubbing (forward = fast grab)
    int frameCapPos = -1;    // index of the last frame retrieved into frameCap

    bool running = true;
    while (running)
    {
        socket_t cl = accept(srv, nullptr, nullptr);
        if (cl == INVALID_SOCKET) continue;

        string req; char buf[4096];
        for (;;)
        {
            int n = recv(cl, buf, sizeof(buf), 0);
            if (n <= 0) break;
            req.append(buf, n);
            if (req.find("\r\n\r\n") != string::npos) break;
        }
        size_t sp1 = req.find(' '), sp2 = req.find(' ', sp1 + 1);
        string target = (sp1 != string::npos && sp2 != string::npos) ? req.substr(sp1 + 1, sp2 - sp1 - 1) : "/";
        string path = target, query;
        size_t qm = target.find('?');
        if (qm != string::npos) { path = target.substr(0, qm); query = target.substr(qm + 1); }

        string status = "200 OK", ctype = "text/plain", body;
        if (path == "/")
        {
            ctype = "text/html; charset=utf-8";
            body = html;
        }
        else if (path == "/stitch")
        {
            if (g_busy) { body = "busy"; }
            else
            {
                Align a;
                a.shiftTop = query.find("shifttop=") != string::npos ? stod(qparam(query, "shifttop")) : 0;
                a.shiftBottom = query.find("shiftbottom=") != string::npos ? stod(qparam(query, "shiftbottom")) : 0;
                a.shiftY = query.find("shifty=") != string::npos ? stod(qparam(query, "shifty")) : 0;
                a.bands = query.find("bands=") != string::npos ? stoi(qparam(query, "bands")) : 0;
                a.exposure = qparam(query, "exposure") == "1";
                a.smartSeam = qparam(query, "smartseam") == "1";
                string ss = qparam(query, "seam");
                StitchMaps mm = m;
                if (!ss.empty()) mm.seam = stoi(ss);
                g_busy = true; g_done = false; g_percent = 0;
                { lock_guard<mutex> lk(g_mu); g_result.clear(); }
                cout << "[stitch] top=" << a.shiftTop << " bottom=" << a.shiftBottom
                     << " y=" << a.shiftY << " seam=" << mm.seam << " ...\n";
                int tf = totalFrames;
                std::thread([mm, a, source, video, degrees, startFrame, endFrame, tf, outDir]() mutable {
                    string res = video
                        ? stitchVideoFile(source, mm, degrees, a, startFrame, endFrame, tf, outDir, &g_percent)
                        : stitchImageFile(source, mm, degrees, a, outDir);
                    { lock_guard<mutex> lk(g_mu); g_result = res; }
                    g_percent = 100; g_done = true; g_busy = false;
                    cout << "[stitch] done -> " << res << "\n";
                }).detach();
                body = "started";
            }
        }
        else if (path == "/frame")
        {
            ctype = "application/json";
            int n = 0; string ns = qparam(query, "n");
            if (!ns.empty()) n = stoi(ns);
            if (n < 0) n = 0;
            Mat frame;
            // sequential positioning (seeking is unreliable). Grab forward from the
            // current position; only re-open when scrubbing backward.
            if (!frameCap.isOpened() || n < frameCapPos)
            { frameCap.release(); frameCap.open(source); frameCapPos = -1; }
            while (frameCapPos < n) { if (!frameCap.grab()) break; frameCapPos++; }
            if (frameCapPos == n) frameCap.retrieve(frame);
            if (frame.empty()) { body = "{\"error\":\"cannot read frame\"}"; }
            else
            {
                UMat uF, wL, wR; frame.copyTo(uF);
                warpHalves(uF, m, wL, wR);
                Mat mL, mR; wL.copyTo(mL); wR.copyTo(mR);
                vector<uchar> bL, bR; vector<int> q = {IMWRITE_JPEG_QUALITY, 85};
                imencode(".jpg", mL, bL, q);
                imencode(".jpg", mR, bR, q);
                ostringstream j;
                j << "{\"left\":\"data:image/jpeg;base64," << base64(bL)
                  << "\",\"right\":\"data:image/jpeg;base64," << base64(bR) << "\"}";
                body = j.str();
            }
        }
        else if (path == "/progress")
        {
            ctype = "application/json";
            string res; { lock_guard<mutex> lk(g_mu); res = g_result; }
            ostringstream j;
            j << "{\"busy\":" << (g_busy ? "true" : "false")
              << ",\"done\":" << (g_done ? "true" : "false")
              << ",\"percent\":" << g_percent.load()
              << ",\"result\":\"" << res << "\"}";
            body = j.str();
        }
        else if (path == "/quit")
        {
            body = "bye";
            running = false;
        }
        else { status = "404 Not Found"; body = "not found"; }

        string resp = "HTTP/1.1 " + status + "\r\nContent-Type: " + ctype +
                      "\r\nContent-Length: " + to_string(body.size()) +
                      "\r\nConnection: close\r\n\r\n" + body;
        send(cl, resp.data(), (int)resp.size(), 0);
        CLOSESOCK(cl);
    }
    frameCap.release();
    CLOSESOCK(srv);
#ifdef _WIN32
    WSACleanup();
#endif
    cout << "Tuner stopped.\n";
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

// Robust frame count when OpenCV's CAP_PROP_FRAME_COUNT is bogus (e.g. MJPEG-MKV).
// Prefer counting demuxed video packets via ffprobe: fast (no decode) and exact for
// intra-only streams like MJPEG (1 packet = 1 frame). Fall back to duration x fps.
static string runCmd(const string &cmd)
{
    FILE *p = popen((cmd + " 2>/dev/null").c_str(), "r");
    if (!p) return "";
    char buf[128]; string out;
    while (fgets(buf, sizeof(buf), p)) out += buf;
    pclose(p);
    return out;
}

static int probeFrames(const string &source, double fps)
{
    string packets = runCmd("ffprobe -v error -count_packets -select_streams v:0 "
                            "-show_entries stream=nb_read_packets "
                            "-of default=nokey=1:noprint_wrappers=1 \"" + source + "\"");
    try { int n = stoi(packets); if (n > 0) return n; } catch (...) {}
    string dur = runCmd("ffprobe -v error -show_entries format=duration "
                        "-of default=nokey=1:noprint_wrappers=1 \"" + source + "\"");
    try { return (int)llround(stod(dur) * fps); } catch (...) { return 0; }
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
    string sx = argVal(argc, argv, "--shift-x", "0");   // convenience: sets top=bottom
    Align a;
    a.shiftTop = stod(argVal(argc, argv, "--shift-top", sx));
    a.shiftBottom = stod(argVal(argc, argv, "--shift-bottom", sx));
    a.shiftY = stod(argVal(argc, argv, "--shift-y", "0"));
    a.bands = stoi(argVal(argc, argv, "--bands", "6"));   // 0 = hard seam
    a.exposure = hasArg(argc, argv, "--exposure");
    a.smartSeam = hasArg(argc, argv, "--smart-seam");
    int port = stoi(argVal(argc, argv, "--port", "8090"));
    bool tune = hasArg(argc, argv, "--tune");
    if (source.empty())
    {
        cerr << "Usage: StitchPipeline --source <image-or-video> [--calib-dir ..] [--out ..]\n"
             << "        [--start N] [--end N] [--degrees D] [--seam X] [--tune] [--port N]\n"
             << "        [--shift-top N] [--shift-bottom N] [--shift-y N]  (--shift-x N sets top=bottom)\n";
        return 1;
    }
    fs::create_directories(outDir);

    ocl::setUseOpenCL(true);
    cout << "OpenCL available: " << ocl::haveOpenCL() << ", using GPU: " << ocl::useOpenCL() << "\n";

    Mat KL, KR, R;
    vector<double> DL, DR;
    loadIntrinsics(calibDir + "/left_intrinsics.json", KL, DL);
    loadIntrinsics(calibDir + "/right_intrinsics.json", KR, DR);
    R = loadRotation(calibDir + "/stereo_extrinsics.json");

    bool video = isVideoFile(source);

    Mat frame;
    int totalFrames = 1;
    if (!video) frame = imread(source);
    else
    {
        VideoCapture cap(source);
        if (cap.isOpened())
        {
            totalFrames = (int)cap.get(CAP_PROP_FRAME_COUNT);
            // some containers (e.g. MJPEG-in-MKV) don't report a valid count;
            // fall back to ffprobe (duration x fps), or 0 -> typeable frame box.
            if (totalFrames < 1 || totalFrames > 100000000)
            {
                double fps = cap.get(CAP_PROP_FPS);
                totalFrames = probeFrames(source, fps > 0 ? fps : 30.0);
            }
            if (startFrame > 0) seekFrame(cap, startFrame);
            cap.read(frame);
            cap.release();
        }
    }
    if (frame.empty()) { cerr << "Cannot read source: " << source << endl; return 1; }

    StitchMaps m = buildStitchMaps(KL, DL, KR, DR, R, frame.cols / 2, frame.rows, seamArg);

    if (tune)
    {
        UMat uFrame, warpL, warpR;
        frame.copyTo(uFrame);
        warpHalves(uFrame, m, warpL, warpR);
        runTuneServer(source, video, m, warpL, warpR, degrees, startFrame, endFrame, outDir,
                      totalFrames, port);
        return 0;
    }

    string result = video ? stitchVideoFile(source, m, degrees, a, startFrame, endFrame, totalFrames, outDir)
                          : stitchImageFile(source, m, degrees, a, outDir);
    cout << (video ? "video -> " : "image -> ") << result << "\n";
    return 0;
}
