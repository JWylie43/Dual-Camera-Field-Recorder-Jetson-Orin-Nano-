// stitch_pipeline.cpp - calibration-driven cylindrical stitch (C++), NO feature detection.
//
// C++ counterpart of stitch_pipeline.py. Reads the rig calibration (left/right
// intrinsics + stereo extrinsics) and stitches one combined LEFT|RIGHT frame into a
// cylindrical panorama, aligning the cameras purely from the extrinsic rotation R.
// There is no BRISK / matcher / findHomography anywhere.
//
// Per camera it folds distortion + cylindrical projection + rotation into ONE remap,
// then composites the two halves with a hard seam (like the original processAndStitch).
//
// Uses only core/imgproc/imgcodecs, so it builds on both OpenCV 4.x and 5.x.
//
// Build:  cmake -S . -B build && cmake --build build
// Run:    ./build/StitchPipeline --image ../calibration/images/calib_114.jpg \
//             --calib-dir ../calibration --out pipeline_out_cpp

#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/imgcodecs.hpp>
#include <iostream>
#include <fstream>
#include <filesystem>
#include <vector>
#include <cmath>
#include <string>
#include "json.hpp"

using json = nlohmann::json;
using namespace std;
using namespace cv;
namespace fs = std::filesystem;

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
// R_cam_from_left = identity for the left camera, extrinsic R for the right.
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

static string argVal(int argc, char **argv, const string &key, const string &def)
{
    for (int i = 1; i < argc - 1; i++)
        if (key == argv[i]) return argv[i + 1];
    return def;
}

int main(int argc, char **argv)
{
    string imagePath = argVal(argc, argv, "--image", "");
    string calibDir = argVal(argc, argv, "--calib-dir", "../calibration");
    string outDir = argVal(argc, argv, "--out", "pipeline_out_cpp");
    double degrees = stod(argVal(argc, argv, "--degrees", "0"));
    int seamArg = stoi(argVal(argc, argv, "--seam", "-1"));
    if (imagePath.empty()) { cerr << "Usage: StitchPipeline --image <combined.jpg> [--calib-dir ..] [--out ..]\n"; return 1; }
    fs::create_directories(outDir);

    Mat img = imread(imagePath);
    if (img.empty()) { cerr << "Cannot read image: " << imagePath << endl; return 1; }
    int w = img.cols / 2, h = img.rows;
    Mat left = img(Rect(0, 0, w, h)).clone();
    Mat right = img(Rect(w, 0, img.cols - w, h)).clone();

    Mat KL, KR, R;
    vector<double> DL, DR;
    loadIntrinsics(calibDir + "/left_intrinsics.json", KL, DL);
    loadIntrinsics(calibDir + "/right_intrinsics.json", KR, DR);
    R = loadRotation(calibDir + "/stereo_extrinsics.json");   // LEFT ray -> RIGHT ray

    // cylinder canvas from FOV + divergence
    double fcyl = KL.at<double>(0, 0);
    double halfL = atan(w / (2 * KL.at<double>(0, 0)));
    double halfR = atan(w / (2 * KR.at<double>(0, 0)));
    double yawR = atan2(R.at<double>(2, 0), R.at<double>(2, 2));   // right axis yaw in left frame
    double pad = 3.0 * CV_PI / 180.0;
    double thetaMin = min(-halfL, yawR - halfR) - pad;
    double thetaMax = max(halfL, yawR + halfR) + pad;
    double vhalf = atan(h / (2 * KL.at<double>(1, 1)));
    int OW = min((int)((thetaMax - thetaMin) * fcyl), 12000);
    int OH = min((int)(2 * tan(vhalf) * fcyl), 4000);
    vector<double> theta(OW), hval(OH);
    for (int i = 0; i < OW; i++) theta[i] = thetaMin + i / fcyl;
    for (int i = 0; i < OH; i++) hval[i] = (i - OH / 2.0) / fcyl;

    cout << "panorama " << OW << "x" << OH
         << ", right yaw " << yawR * 180.0 / CV_PI << " deg, span "
         << (thetaMax - thetaMin) * 180.0 / CV_PI << " deg\n";

    Mat mapLx, mapLy, okL, mapRx, mapRy, okR;
    buildCylMap(KL, DL, Mat::eye(3, 3, CV_64F), theta, hval, w, h, mapLx, mapLy, okL);
    buildCylMap(KR, DR, R, theta, hval, w, h, mapRx, mapRy, okR);

    Mat warpL, warpR;
    remap(left, warpL, mapLx, mapLy, INTER_LINEAR, BORDER_CONSTANT);
    remap(right, warpR, mapRx, mapRy, INTER_LINEAR, BORDER_CONSTANT);

    // hard seam at the centre of the overlap band
    vector<int> overlapCols;
    for (int x = 0; x < OW; x++)
    {
        bool lc = false, rc = false;
        for (int y = 0; y < OH && !(lc && rc); y++)
        { if (okL.at<uchar>(y, x)) lc = true; if (okR.at<uchar>(y, x)) rc = true; }
        if (lc && rc) overlapCols.push_back(x);
    }
    int seam = seamArg >= 0 ? seamArg
               : (overlapCols.empty() ? OW / 2 : overlapCols[overlapCols.size() / 2]);
    cout << "overlap cols [" << (overlapCols.empty() ? 0 : overlapCols.front())
         << ".." << (overlapCols.empty() ? 0 : overlapCols.back())
         << "], hard seam @ " << seam << "\n";

    Mat pano = Mat::zeros(OH, OW, warpL.type());
    warpL(Rect(0, 0, seam, OH)).copyTo(pano(Rect(0, 0, seam, OH)));
    warpR(Rect(seam, 0, OW - seam, OH)).copyTo(pano(Rect(seam, 0, OW - seam, OH)));

    if (degrees != 0.0)
    {
        // build the 2x3 rotation-about-centre matrix by hand (getRotationMatrix2D
        // moved modules in OpenCV 5; this keeps the source portable across 4.x/5.x)
        double ang = degrees * CV_PI / 180.0;
        double a = cos(ang), b = sin(ang), ccx = OW / 2.0, ccy = OH / 2.0;
        Mat M = (Mat_<double>(2, 3) << a, b, (1 - a) * ccx - b * ccy,
                 -b, a, b * ccx + (1 - a) * ccy);
        warpAffine(pano, pano, M, Size(OW, OH));
    }

    Mat overlap;
    addWeighted(warpL, 0.5, warpR, 0.5, 0.0, overlap);
    imwrite(outDir + "/pano.jpg", pano);
    imwrite(outDir + "/overlap_5050.jpg", overlap);
    cout << "saved -> " << outDir << "/pano.jpg  (hard seam @ col " << seam << ")\n";
    cout << "saved -> " << outDir << "/overlap_5050.jpg\n";
    return 0;
}
