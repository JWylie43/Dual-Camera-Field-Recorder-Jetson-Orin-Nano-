#include <opencv2/opencv.hpp>
#include <iostream>
#include <vector>
#include <cmath>
#include <set>
#include <chrono>
#include <fstream>
#include <opencv2/core/ocl.hpp>
#include "json.hpp"
using json = nlohmann::json;

using namespace std;
using namespace cv;

struct homographyMaps
{
    std::pair<cv::Mat, cv::Mat> leftUndistortionMap;
    std::pair<cv::Mat, cv::Mat> rightUndistortionMap;
    Mat homographyMatrix;
};

struct warpMaps
{
    std::pair<cv::Mat, cv::Mat> leftComboMapDebug;
    std::pair<cv::Mat, cv::Mat> rightComboMapDebug;
    std::pair<cv::Mat, cv::Mat> leftComboMap;
    std::pair<cv::Mat, cv::Mat> rightComboMap;
};

// Filter keypoints so that only one survives within each r-pixel radius
std::pair<std::vector<cv::Point2f>, std::vector<cv::Point2f>>
selectPoints(const std::vector<cv::Point2f> &leftPoints,
             const std::vector<cv::Point2f> &rightPoints,
             int width, int height, int r = 30)
{
    cv::Mat mask(height, width, CV_8U, cv::Scalar(0));
    std::set<int> kept;

    for (int i = 0; i < (int)leftPoints.size(); i++)
    {
        int x = (int)leftPoints[i].x;
        int y = (int)leftPoints[i].y;

        if (x < 0 || x >= width || y < 0 || y >= height)
            continue;

        if (mask.at<uchar>(y, x) != 0)
            continue;

        kept.insert(i);
        cv::circle(mask, cv::Point(x, y), r, cv::Scalar(255), -1);
    }

    std::vector<cv::Point2f> newLeft, newRight;
    for (int i : kept)
    {
        newLeft.push_back(leftPoints[i]);
        newRight.push_back(rightPoints[i]);
    }

    return {newLeft, newRight};
}

// Build an (h*w × 3) matrix of homogeneous pixel coordinates
cv::Mat makeHomogeneousGrid(int width, int height)
{
    int total = width * height;

    // Allocate output: each row is [x, y, 1]
    cv::Mat X(total, 3, CV_64F);

    int idx = 0;
    for (int y = 0; y < height; y++)
    {
        for (int x = 0; x < width; x++)
        {
            X.at<double>(idx, 0) = x;
            X.at<double>(idx, 1) = y;
            X.at<double>(idx, 2) = 1.0;
            idx++;
        }
    }
    return X;
}

pair<Mat, Mat> initUndistortCylindrialWarpMap(
    const Mat &K, // Intrinsic matrix (3x3)
    const Mat &C, // Distortion coefficients (at least 5, optionally up to 8)
    const Mat &H, // Homography (3x3) or identity
    int width,
    int height,
    int bufferPixels)
{
    width += bufferPixels;
    height += bufferPixels;

    // Homogeneous coordinates grid
    cv::Mat X = makeHomogeneousGrid(width, height); // (h*w × 3)

    // Apply inverse homography if provided
    Mat Xh = X.t();
    if (!H.empty())
    {
        Mat iH = H.inv();
        Xh = iH * Xh;
    }
    X = Xh.t();
    for (int i = 0; i < X.rows; i++)
    {
        double w_ = X.at<double>(i, 2);
        X.at<double>(i, 0) /= w_;
        X.at<double>(i, 1) /= w_;
    }

    // Inverse intrinsics
    Mat iK = K.inv();
    Xh = (iK * X.t()).t();

    // Cylindrical projection (sin, y, cos)
    Mat A(X.rows, 3, CV_64F);
    for (int i = 0; i < X.rows; i++)
    {
        double x = Xh.at<double>(i, 0);
        double y = Xh.at<double>(i, 1);
        A.at<double>(i, 0) = sin(x);
        A.at<double>(i, 1) = y;
        A.at<double>(i, 2) = cos(x);
    }

    // Project back with K
    Mat B = (K * A.t()).t();
    Mat u = B.col(0).clone();
    Mat v = B.col(1).clone();
    Mat w_ = B.col(2).clone();
    for (int i = 0; i < u.rows; i++)
    {
        u.at<double>(i, 0) /= w_.at<double>(i, 0);
        v.at<double>(i, 0) /= w_.at<double>(i, 0);
    }

    // Reshape back to images
    Mat uImg = u.reshape(1, height);
    Mat vImg = v.reshape(1, height);

    // Extract intrinsics
    double fx = K.at<double>(0, 0);
    double fy = K.at<double>(1, 1);
    double cx = K.at<double>(0, 2);
    double cy = K.at<double>(1, 2);

    // Distortion coefficients
    vector<double> coeffs;
    C.copyTo(coeffs);
    double k1 = coeffs.size() > 0 ? coeffs[0] : 0;
    double k2 = coeffs.size() > 1 ? coeffs[1] : 0;
    double p1 = coeffs.size() > 2 ? coeffs[2] : 0;
    double p2 = coeffs.size() > 3 ? coeffs[3] : 0;
    double k3 = coeffs.size() > 4 ? coeffs[4] : 0;
    double k4 = coeffs.size() > 5 ? coeffs[5] : 0;
    double k5 = coeffs.size() > 6 ? coeffs[6] : 0;
    double k6 = coeffs.size() > 7 ? coeffs[7] : 0;

    // Normalize
    Mat xNorm = (uImg - cx) / fx;
    Mat yNorm = (vImg - cy) / fy;

    Mat x2, y2, r2, _2xy;
    multiply(xNorm, xNorm, x2);
    multiply(yNorm, yNorm, y2);
    r2 = x2 + y2;
    _2xy = 2 * xNorm.mul(yNorm);

    // Radial distortion
    Mat kr = (1 + k1 * r2 + k2 * r2.mul(r2) + k3 * r2.mul(r2).mul(r2)) /
             (1 + k4 * r2 + k5 * r2.mul(r2) + k6 * r2.mul(r2).mul(r2));

    // Apply distortion
    Mat uDist = fx * (xNorm.mul(kr) + p1 * _2xy + p2 * (r2 + 2 * x2)) + cx;
    Mat vDist = fy * (yNorm.mul(kr) + p1 * (r2 + 2 * y2) + p2 * _2xy) + cy;

    // Offset by buffer
    uDist = uDist - (bufferPixels / 2.0);
    vDist = vDist - (bufferPixels / 2.0);

    // Convert to float maps for remap()
    Mat mapX, mapY;
    uDist.convertTo(mapX, CV_32F);
    vDist.convertTo(mapY, CV_32F);

    return {mapX, mapY};
}

void buildWarpMap(const Mat &H, int box_w, int box_h, Mat &mapX, Mat &mapY)
{
    CV_Assert(H.rows == 3 && H.cols == 3);
    Mat H_inv = H.inv();

    // Extract elements once
    double h00 = H_inv.at<double>(0, 0), h01 = H_inv.at<double>(0, 1), h02 = H_inv.at<double>(0, 2);
    double h10 = H_inv.at<double>(1, 0), h11 = H_inv.at<double>(1, 1), h12 = H_inv.at<double>(1, 2);
    double h20 = H_inv.at<double>(2, 0), h21 = H_inv.at<double>(2, 1), h22 = H_inv.at<double>(2, 2);

    mapX.create(box_h, box_w, CV_32FC1);
    mapY.create(box_h, box_w, CV_32FC1);

    for (int y = 0; y < box_h; ++y)
    {
        // Precompute row terms
        double rowX = h01 * y + h02;
        double rowY = h11 * y + h12;
        double rowW = h21 * y + h22;

        float *mapXrow = mapX.ptr<float>(y);
        float *mapYrow = mapY.ptr<float>(y);

        for (int x = 0; x < box_w; ++x)
        {
            double X = h00 * x + rowX;
            double Y = h10 * x + rowY;
            double W = h20 * x + rowW;

            mapXrow[x] = static_cast<float>(X / W);
            mapYrow[x] = static_cast<float>(Y / W);
        }
    }
}

std::pair<
    std::pair<cv::Mat, cv::Mat>, // left warp maps
    std::pair<cv::Mat, cv::Mat>  // right warp maps
    >
buildWarpMaps(
    int rows, int cols,
    const Mat &H,
    const Mat &M,
    int box_x,
    int box_y,
    int box_w,
    int box_h)
{
    Mat T = (Mat_<double>(3, 3) << 1, 0, 0,
             0, 1, rows / 2.0,
             0, 0, 1);
    Mat crop_T = (Mat_<double>(3, 3) << 1.0, 0.0, -box_x,
                  0.0, 1.0, -box_y,
                  0.0, 0.0, 1.0);
    Mat H_left = crop_T * M * T;
    Mat H_right = crop_T * M * T * H;
    Mat mapXL, mapXR, mapYL, mapYR;
    std::chrono::high_resolution_clock::time_point start = std::chrono::high_resolution_clock::now();
    cout << "starting map " << std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::high_resolution_clock::now() - start).count() << " ms" << endl;
    buildWarpMap(H_left, box_w, box_h, mapXL, mapYL);
    cout << "map built in " << std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::high_resolution_clock::now() - start).count() << " ms" << endl;
    buildWarpMap(H_right, box_w, box_h, mapXR, mapYR);
    cout << "map built in " << std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::high_resolution_clock::now() - start).count() << " ms" << endl;
    return {
        {mapXL, mapYL}, // leftWarpMap
        {mapXR, mapYR}  // rightWarpMap
    };
}

std::pair<cv::Mat, cv::Mat> combineUndistortionAndWarp(
    const cv::Mat &warpX, const cv::Mat &warpY,           // from buildWarpMap()
    const cv::Mat &undistortX, const cv::Mat &undistortY) // from initUndistortCylindrialWarpMap()
{
    CV_Assert(warpX.size() == warpY.size());
    cv::Mat finalX(warpX.size(), CV_32F);
    cv::Mat finalY(warpY.size(), CV_32F);

    for (int y = 0; y < warpX.rows; ++y)
    {
        const float *wx = warpX.ptr<float>(y);
        const float *wy = warpY.ptr<float>(y);
        float *fx = finalX.ptr<float>(y);
        float *fy = finalY.ptr<float>(y);

        for (int x = 0; x < warpX.cols; ++x)
        {
            float u = wx[x];
            float v = wy[x];

            if (u >= 0 && u < undistortX.cols && v >= 0 && v < undistortX.rows)
            {
                fx[x] = undistortX.at<float>(v, u);
                fy[x] = undistortY.at<float>(v, u);
            }
            else
            {
                fx[x] = fy[x] = -1.0f; // invalid region
            }
        }
    }
    return {finalX, finalY};
}

void processAndStitchVideos(
    const string &left_video_path,
    const string &right_video_path,
    const string &output_path,
    const std::pair<cv::Mat, cv::Mat> finalLeftMap,
    const std::pair<cv::Mat, cv::Mat> finalRightMap,
    int seam_x,
    int box_w,
    int box_h,
    int box_x,
    int box_y,
    std::vector<std::pair<int, int>> frameRanges)
{
    cv::ocl::setUseOpenCL(true);
    cout << "OpenCL available: " << cv::ocl::haveOpenCL() << endl;
    cout << "OpenCL used: " << cv::ocl::useOpenCL() << endl;
    // --- Open video inputs ---
    VideoCapture capL(left_video_path);
    VideoCapture capR(right_video_path);

    if (!capL.isOpened() || !capR.isOpened())
    {
        cerr << "Error: Could not open input videos." << endl;
        return;
    }

    double fps = capL.get(CAP_PROP_FPS);
    int fourcc = VideoWriter::fourcc('m', 'p', '4', 'v'); // H.264

    // --- Video writer ---
    VideoWriter writer(output_path, fourcc, fps, Size(box_w, box_h));
    if (!writer.isOpened())
    {
        cerr << "Error: Could not open output video for writing." << endl;
        return;
    }
    int totalFrames = static_cast<int>(
        std::min(
            capL.get(cv::CAP_PROP_FRAME_COUNT),
            capR.get(cv::CAP_PROP_FRAME_COUNT)));

    // int totalFrames = 100;

    Mat frameL,
        frameR;
    cv::UMat uFrameL, uFrameR, uWarpedL, uWarpedR, uStitched;
    std::chrono::high_resolution_clock::time_point start = std::chrono::high_resolution_clock::now();

    int currentRange = 0;
    int totalRanges = static_cast<int>(frameRanges.size());
    int shiftedSeam = seam_x - box_x;
    if (totalRanges == 0)
    {
        cout << "[WARN] No frame ranges provided — processing full video." << endl;
        frameRanges.push_back({0, totalFrames - 1});
        totalRanges = 1;
    }

    for (int frameIndex = frameRanges[0].first; frameIndex < totalFrames && currentRange < totalRanges;)
    {
        auto [startF, endF] = frameRanges[currentRange];

        // --- Seek to start of current range ---
        capL.set(cv::CAP_PROP_POS_FRAMES, startF);
        capR.set(cv::CAP_PROP_POS_FRAMES, startF);
        frameIndex = startF;

        cout << "\n[INFO] Processing range " << (currentRange + 1)
             << "/" << totalRanges << " (" << startF << " → " << endF << ")" << endl;

        // --- Process frames within this range ---
        for (; frameIndex <= endF && frameIndex < totalFrames; ++frameIndex)
        {
            if (!capL.read(frameL) || !capR.read(frameR))
                break;

            auto startFrame = std::chrono::high_resolution_clock::now();

            // GPU remap & stitch
            frameL.copyTo(uFrameL);
            frameR.copyTo(uFrameR);

            // try cropping while warping
            cv::Rect roiLeft(0, 0, shiftedSeam, box_h);
            cv::Rect roiRight(shiftedSeam, 0, box_w - shiftedSeam, box_h);
            cv::remap(uFrameL, uWarpedL, finalLeftMap.first(roiLeft), finalLeftMap.second(roiLeft), cv::INTER_LINEAR);
            cv::remap(uFrameR, uWarpedR, finalRightMap.first(roiRight), finalRightMap.second(roiRight), cv::INTER_LINEAR);

            uStitched = cv::UMat(cv::Size(box_w, box_h), uWarpedL.type());

            uWarpedL.copyTo(uStitched(cv::Rect(0, 0, uWarpedL.cols, box_h)));
            uWarpedR.copyTo(uStitched(cv::Rect(uWarpedL.cols, 0, uWarpedR.cols, box_h)));

            // cv::remap(uFrameL, uWarpedL, finalLeftMap.first, finalLeftMap.second, cv::INTER_LINEAR);
            // cv::remap(uFrameR, uWarpedR, finalRightMap.first, finalRightMap.second, cv::INTER_LINEAR);

            // uWarpedL(cv::Rect(0, 0, shiftedSeam, box_h))
            //     .copyTo(uStitched(cv::Rect(0, 0, shiftedSeam, box_h)));
            // uWarpedR(cv::Rect(shiftedSeam, 0, box_w - shiftedSeam, box_h))
            //     .copyTo(uStitched(cv::Rect(shiftedSeam, 0, box_w - shiftedSeam, box_h)));

            cv::Mat out;
            uStitched.copyTo(out);
            writer.write(out);

            if (frameIndex < startF + 5)
            {
                cout << "  Frame " << frameIndex
                     << " remapped in "
                     << std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::high_resolution_clock::now() - startFrame)
                            .count()
                     << " ms" << endl;
            }

            if (frameIndex % 30 == 0 || frameIndex == endF)
            {
                double progress = 100.0 * (frameIndex - startF + 1) / (endF - startF + 1);
                cout << "  Range progress: " << static_cast<int>(progress) << "%" << endl;
            }
        }

        // Move to next range
        ++currentRange;
    }

    // for (int frameIndex = 0; frameIndex < totalFrames; frameIndex++)
    // {
    //     if (!capL.read(frameL) || !capR.read(frameR))
    //         break;

    //     std::chrono::high_resolution_clock::time_point startFrame = std::chrono::high_resolution_clock::now();

    //     // gpu
    //     frameL.copyTo(uFrameL);
    //     frameR.copyTo(uFrameR);
    //     cv::remap(uFrameL, uWarpedL, finalLeftMap.first, finalLeftMap.second, cv::INTER_LINEAR);
    //     cv::remap(uFrameR, uWarpedR, finalRightMap.first, finalRightMap.second, cv::INTER_LINEAR);
    //     int shiftedSeam = seam_x - box_x;
    //     uStitched = cv::UMat(cv::Size(box_w, box_h), uWarpedL.type());
    //     uWarpedL(cv::Rect(0, 0, shiftedSeam, box_h)).copyTo(uStitched(cv::Rect(0, 0, shiftedSeam, box_h)));
    //     uWarpedR(cv::Rect(shiftedSeam, 0, box_w - shiftedSeam, box_h))
    //         .copyTo(uStitched(cv::Rect(shiftedSeam, 0, box_w - shiftedSeam, box_h)));

    //     cv::Mat out;
    //     uStitched.copyTo(out);
    //     writer.write(out);

    //     if (frameIndex < 5)
    //     {
    //         cout << "Frame " << frameIndex << " remapped in " << std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::high_resolution_clock::now() - startFrame).count() << " ms" << endl;
    //     }

    //     // --- Log progress ---
    //     if (frameIndex % 30 == 0 || frameIndex == totalFrames - 1)
    //     {
    //         double progress = 100.0 * (frameIndex + 1) / totalFrames;
    //         cout << "PROGRESS: " << static_cast<int>(progress) << "%" << endl;
    //     }
    // }

    capL.release();
    capR.release();
    writer.release();
    cout << "Finished in " << (std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::high_resolution_clock::now() - start).count()) / 1000 << " s" << endl;
    cout << "Saved stitched video to " << output_path << endl;
}

void testWarpPipeline2(
    const std::string &output_directory,
    const string &leftVideoPath,
    const string &rightVideoPath,
    homographyMaps &homographyMapsResults, double degrees, int seam_x,
    int box_x, int box_y, int box_w, int box_h)
{

    // Rotation
    int undistortedRows = homographyMapsResults.leftUndistortionMap.first.rows;
    int undistortedCols = homographyMapsResults.leftUndistortionMap.first.cols;
    // get warped/rotated image on large canvas
    Point2f center(undistortedCols, undistortedRows);
    Mat affineMatrix = getRotationMatrix2D(center, degrees, 1.0); // 2x3
    Mat rotationMatrix = Mat::eye(3, 3, CV_64F);
    affineMatrix.copyTo(rotationMatrix(Rect(0, 0, 3, 2))); // place in top 2 rows

    // build complete/debug warp maps
    std::pair<std::pair<cv::Mat, cv::Mat>, std::pair<cv::Mat, cv::Mat>> warpMapsDebug = buildWarpMaps(undistortedRows, undistortedCols, homographyMapsResults.homographyMatrix, rotationMatrix, 0, 0, undistortedCols * 2, undistortedRows * 2);
    std::pair<cv::Mat, cv::Mat> leftWarpMapDebug = warpMapsDebug.first;
    std::pair<cv::Mat, cv::Mat> rightWarpMapDebug = warpMapsDebug.second;
    std::pair<cv::Mat, cv::Mat> leftComboMapDebug =
        combineUndistortionAndWarp(leftWarpMapDebug.first, leftWarpMapDebug.second, homographyMapsResults.leftUndistortionMap.first, homographyMapsResults.leftUndistortionMap.second);
    std::pair<cv::Mat, cv::Mat> rightComboMapDebug =
        combineUndistortionAndWarp(rightWarpMapDebug.first, rightWarpMapDebug.second, homographyMapsResults.rightUndistortionMap.first, homographyMapsResults.rightUndistortionMap.second);

    std::pair<std::pair<cv::Mat, cv::Mat>, std::pair<cv::Mat, cv::Mat>> warpMaps = buildWarpMaps(undistortedRows, undistortedCols, homographyMapsResults.homographyMatrix, rotationMatrix, box_x, box_y, box_w, box_h);
    std::pair<cv::Mat, cv::Mat> leftWarpMap = warpMaps.first;
    std::pair<cv::Mat, cv::Mat> rightWarpMap = warpMaps.second;
    std::pair<cv::Mat, cv::Mat> leftComboMap =
        combineUndistortionAndWarp(leftWarpMap.first, leftWarpMap.second, homographyMapsResults.leftUndistortionMap.first, homographyMapsResults.leftUndistortionMap.second);
    std::pair<cv::Mat, cv::Mat> rightComboMap =
        combineUndistortionAndWarp(rightWarpMap.first, rightWarpMap.second, homographyMapsResults.rightUndistortionMap.first, homographyMapsResults.rightUndistortionMap.second);

    VideoCapture capL(leftVideoPath);
    VideoCapture capR(rightVideoPath);
    Mat distortedL, distortedR;
    capL.set(cv::CAP_PROP_POS_FRAMES, 684);
    capR.set(cv::CAP_PROP_POS_FRAMES, 684);
    capL.read(distortedL);
    capR.read(distortedR);
    capL.release();
    capR.release();

    // ========== 1. One-pass undistort + warp + rotate & crop with large canvas ==========
    Mat debugWarpedLeft, debugWarpedRight;
    remap(distortedL, debugWarpedLeft, leftComboMapDebug.first, leftComboMapDebug.second, INTER_LINEAR, BORDER_CONSTANT);
    remap(distortedR, debugWarpedRight, rightComboMapDebug.first, rightComboMapDebug.second, INTER_LINEAR, BORDER_CONSTANT);
    // imwrite("debugWarpedLeft.jpg", debugWarpedLeft);
    // imwrite("debugWarpedRight.jpg", debugWarpedRight);

    Mat debugOverlapped;
    addWeighted(debugWarpedLeft, 0.5, debugWarpedRight, 0.5, 0.0, debugOverlapped);
    line(debugOverlapped, Point(seam_x, 0),
         Point(seam_x, debugOverlapped.rows), Scalar(0, 0, 255), 3);
    rectangle(debugOverlapped, Rect(box_x, box_y, box_w, box_h), Scalar(0, 255, 0), 3);

    imwrite(output_directory + "\\debugOverlapped.jpg", debugOverlapped);

    int shiftedSeam = seam_x - box_x;
    // ========== 2. One-pass undistort + warp + rotate & crop ==========
    // Mat finalWarpedLeft, finalWarpedRight;
    // remap(distortedL, finalWarpedLeft, leftComboMap.first, leftComboMap.second, INTER_LINEAR, BORDER_CONSTANT);
    // remap(distortedR, finalWarpedRight, rightComboMap.first, rightComboMap.second, INTER_LINEAR, BORDER_CONSTANT);
    // // imwrite("finalWarpedLeft.jpg", finalWarpedLeft);
    // // imwrite("finalWarpedRight.jpg", finalWarpedRight);

    // Mat finalOverlappedCropped;
    // addWeighted(finalWarpedLeft, 0.5, finalWarpedRight, 0.5, 0.0, finalOverlappedCropped);
    // line(finalOverlappedCropped, Point(shiftedSeam, 0),
    //      Point(shiftedSeam, box_h), Scalar(0, 0, 255), 3);
    // imwrite(output_directory + "\\finalOverlappedCropped.jpg", finalOverlappedCropped);
    // Mat finalStitched;
    // finalStitched = cv::Mat(cv::Size(box_w, box_h), finalWarpedLeft.type());
    // finalWarpedLeft(cv::Rect(0, 0, shiftedSeam, box_h))
    //     .copyTo(finalStitched(cv::Rect(0, 0, shiftedSeam, box_h)));
    // finalWarpedRight(cv::Rect(shiftedSeam, 0, box_w - shiftedSeam, box_h))
    //     .copyTo(finalStitched(cv::Rect(shiftedSeam, 0, box_w - shiftedSeam, box_h)));
    // imwrite(output_directory + "\\finalStitched.jpg", finalStitched);

    // ========== 3. One-pass undistort + warp + rotate + crop ==========
    Mat warpedCroppedLeft, warpedCroppedRight;
    cv::Rect roiLeft(0, 0, shiftedSeam, box_h);
    cv::Rect roiRight(shiftedSeam, 0, box_w - shiftedSeam, box_h);
    remap(distortedL, warpedCroppedLeft, leftComboMap.first(roiLeft), leftComboMap.second(roiLeft), INTER_LINEAR, BORDER_CONSTANT);
    remap(distortedR, warpedCroppedRight, rightComboMap.first(roiRight), rightComboMap.second(roiRight), INTER_LINEAR, BORDER_CONSTANT);
    // imwrite(output_directory + "\\warpedCroppedLeft.jpg", warpedCroppedLeft);
    // imwrite(output_directory + "\\warpedCroppedRight.jpg", warpedCroppedRight);
    cv::Mat stitched(box_h, box_w, warpedCroppedLeft.type(), cv::Scalar::all(0));
    warpedCroppedLeft.copyTo(stitched(cv::Rect(0, 0, warpedCroppedLeft.cols, box_h)));
    warpedCroppedRight.copyTo(stitched(cv::Rect(shiftedSeam, 0, warpedCroppedRight.cols, box_h)));
    imwrite(output_directory + "\\stitchedFrame.jpg", stitched);

    std::cout << "[testWarpPipeline] Complete — wrote all intermediate images.\n";
}

void testWarpPipeline(
    const std::string output_directory,
    const cv::Mat &distortedL,
    const cv::Mat &distortedR,
    const std::pair<cv::Mat, cv::Mat> &leftComboMapDebug,
    const std::pair<cv::Mat, cv::Mat> &rightComboMapDebug,
    const std::pair<cv::Mat, cv::Mat> &leftComboMap,
    const std::pair<cv::Mat, cv::Mat> &rightComboMap,
    int seam_x,
    int box_x, int box_y, int box_w, int box_h)
{

    int rows = distortedL.rows;
    int cols = distortedL.cols;

    // // ========== 1. Basic large canvas warp ==========
    // Mat T = (Mat_<double>(3, 3) << 1, 0, 0,
    //          0, 1, rows / 2.0,
    //          0, 0, 1);
    // int largeRows = rows * 2;
    // int largeCols = cols * 2;

    // Mat warpedLeftLarge, warpedRightLarge;
    // warpPerspective(undistortedL, warpedLeftLarge, T, Size(largeCols, largeRows));
    // warpPerspective(undistortedR, warpedRightLarge, T * homographyMatrix, Size(largeCols, largeRows));
    // imwrite("warped_left_large.jpg", warpedLeftLarge);
    // imwrite("warped_right_large.jpg", warpedRightLarge);

    // // ========== 2. Add rotation ==========
    // Mat warpedLeftLargeRotated, warpedRightLargeRotated;
    // warpPerspective(undistortedL, warpedLeftLargeRotated, rotationMatrix * T, Size(largeCols, largeRows));
    // warpPerspective(undistortedR, warpedRightLargeRotated, rotationMatrix * T * homographyMatrix, Size(largeCols, largeRows));
    // imwrite("warped_left_large_rotated.jpg", warpedLeftLargeRotated);
    // imwrite("warped_right_large_rotated.jpg", warpedRightLargeRotated);

    // // ========== 3. Overlay visualization ==========
    // Mat overlapped;
    // addWeighted(warpedLeftLargeRotated, 0.5, warpedRightLargeRotated, 0.5, 0.0, overlapped);

    // line(overlapped, Point(seam_x, 0), Point(seam_x, largeRows), Scalar(0, 0, 255), 3);
    // rectangle(overlapped, Rect(box_x, box_y, box_w, box_h), Scalar(0, 255, 0), 3);
    // imwrite("overlapped.jpg", overlapped);

    // // ========== 4. Cropped output ==========
    // Mat crop_T = (Mat_<double>(3, 3) << 1.0, 0.0, -box_x,
    //               0.0, 1.0, -box_y,
    //               0.0, 0.0, 1.0);

    // Mat warpedLeftCropped, warpedRightCropped;
    // warpPerspective(undistortedL, warpedLeftCropped, crop_T * rotationMatrix * T, Size(box_w, box_h));
    // warpPerspective(undistortedR, warpedRightCropped, crop_T * rotationMatrix * T * homographyMatrix, Size(box_w, box_h));
    // imwrite("warped_left_cropped.jpg", warpedLeftCropped);
    // imwrite("warped_right_cropped.jpg", warpedRightCropped);

    // Mat overlappedCropped;
    // addWeighted(warpedLeftCropped, 0.5, warpedRightCropped, 0.5, 0.0, overlappedCropped);
    // line(overlappedCropped, Point(seam_x - box_x, 0),
    //      Point(seam_x - box_x, box_h), Scalar(0, 0, 255), 3);
    // imwrite("overlapped_cropped.jpg", overlappedCropped);

    // ========== 5. One-pass undistort + warp + rotate + crop with large canvas ==========
    Mat debugWarpedLeft, debugWarpedRight;
    remap(distortedL, debugWarpedLeft, leftComboMapDebug.first, leftComboMapDebug.second, INTER_LINEAR, BORDER_CONSTANT);
    remap(distortedR, debugWarpedRight, rightComboMapDebug.first, rightComboMapDebug.second, INTER_LINEAR, BORDER_CONSTANT);
    // imwrite("debugWarpedLeft.jpg", debugWarpedLeft);
    // imwrite("debugWarpedRight.jpg", debugWarpedRight);

    Mat debugOverlapped;
    addWeighted(debugWarpedLeft, 0.5, debugWarpedRight, 0.5, 0.0, debugOverlapped);
    line(debugOverlapped, Point(seam_x, 0),
         Point(seam_x, rows * 2), Scalar(0, 0, 255), 3);
    rectangle(debugOverlapped, Rect(box_x, box_y, box_w, box_h), Scalar(0, 255, 0), 3);

    imwrite(output_directory + "\\debugOverlapped.jpg", debugOverlapped);

    // ========== 6. One-pass undistort + warp + rotate + crop ==========
    Mat finalWarpedLeft, finalWarpedRight;
    remap(distortedL, finalWarpedLeft, leftComboMap.first, leftComboMap.second, INTER_LINEAR, BORDER_CONSTANT);
    remap(distortedR, finalWarpedRight, rightComboMap.first, rightComboMap.second, INTER_LINEAR, BORDER_CONSTANT);
    // imwrite("finalWarpedLeft.jpg", finalWarpedLeft);
    // imwrite("finalWarpedRight.jpg", finalWarpedRight);
    int shiftedSeam = seam_x - box_x;
    Mat finalOverlappedCropped;
    addWeighted(finalWarpedLeft, 0.5, finalWarpedRight, 0.5, 0.0, finalOverlappedCropped);
    line(finalOverlappedCropped, Point(shiftedSeam, 0),
         Point(shiftedSeam, box_h), Scalar(0, 0, 255), 3);
    imwrite(output_directory + "\\finalOverlappedCropped.jpg", finalOverlappedCropped);

    // testing warp and crop together
    Mat warpedCroppedLeft, warpedCroppedRight;
    cv::Rect roiLeft(0, 0, shiftedSeam, box_h);
    cv::Rect roiRight(shiftedSeam, 0, box_w - shiftedSeam, box_h);
    remap(distortedL, warpedCroppedLeft, leftComboMap.first(roiLeft), leftComboMap.second(roiLeft), INTER_LINEAR, BORDER_CONSTANT);
    remap(distortedR, warpedCroppedRight, rightComboMap.first(roiRight), rightComboMap.second(roiRight), INTER_LINEAR, BORDER_CONSTANT);
    imwrite(output_directory + "\\warpedCroppedLeft.jpg", warpedCroppedLeft);
    imwrite(output_directory + "\\warpedCroppedRight.jpg", warpedCroppedRight);
    cv::Mat stitched(box_h, box_w, warpedCroppedLeft.type(), cv::Scalar::all(0));
    warpedCroppedLeft.copyTo(stitched(cv::Rect(0, 0, warpedCroppedLeft.cols, box_h)));
    warpedCroppedRight.copyTo(stitched(cv::Rect(shiftedSeam, 0, warpedCroppedRight.cols, box_h)));
    imwrite(output_directory + "\\stitchedFrame.jpg", stitched);

    // final stitched frame
    Mat finalStitched;
    finalStitched = cv::Mat(cv::Size(box_w, box_h), finalWarpedLeft.type());
    finalWarpedLeft(cv::Rect(0, 0, shiftedSeam, box_h))
        .copyTo(finalStitched(cv::Rect(0, 0, shiftedSeam, box_h)));
    finalWarpedRight(cv::Rect(shiftedSeam, 0, box_w - shiftedSeam, box_h))
        .copyTo(finalStitched(cv::Rect(shiftedSeam, 0, box_w - shiftedSeam, box_h)));
    imwrite(output_directory + "\\finalStitched.jpg", finalStitched);

    // Mat finalStitchedBlended;
    // finalStitchedBlended = cv::Mat(cv::Size(box_w, box_h), finalWarpedLeft.type());
    // int blendWidth = 100; // adjust: 50–200 px works well
    // int leftEnd = max(0, shiftedSeam - blendWidth / 2);
    // int rightStart = min(box_w, shiftedSeam + blendWidth / 2);

    // finalWarpedLeft(cv::Rect(0, 0, leftEnd, box_h))
    //     .copyTo(finalStitchedBlended(cv::Rect(0, 0, leftEnd, box_h)));
    // finalWarpedRight(cv::Rect(rightStart, 0, box_w - rightStart, box_h))
    //     .copyTo(finalStitchedBlended(cv::Rect(rightStart, 0, box_w - rightStart, box_h)));

    // // 3️⃣ Blend overlapping band
    // int blendWidthActual = rightStart - leftEnd;
    // if (blendWidthActual > 0)
    // {
    //     Mat leftStrip = finalWarpedLeft(Rect(leftEnd, 0, blendWidthActual, box_h));
    //     Mat rightStrip = finalWarpedRight(Rect(leftEnd, 0, blendWidthActual, box_h));
    //     Mat blendedStrip(box_h, blendWidthActual, leftStrip.type());

    //     // horizontal gradient alpha mask
    //     for (int x = 0; x < blendWidthActual; ++x)
    //     {
    //         float alpha = static_cast<float>(x) / (blendWidthActual - 1); // 0→1
    //         float beta = 1.0f - alpha;
    //         // blend each column across all rows
    //         for (int y = 0; y < box_h; ++y)
    //         {
    //             Vec3b lPix = leftStrip.at<Vec3b>(y, x);
    //             Vec3b rPix = rightStrip.at<Vec3b>(y, x);
    //             blendedStrip.at<Vec3b>(y, x) = Vec3b(
    //                 saturate_cast<uchar>(beta * lPix[0] + alpha * rPix[0]),
    //                 saturate_cast<uchar>(beta * lPix[1] + alpha * rPix[1]),
    //                 saturate_cast<uchar>(beta * lPix[2] + alpha * rPix[2]));
    //         }
    //     }

    //     blendedStrip.copyTo(finalStitchedBlended(Rect(leftEnd, 0, blendWidthActual, box_h)));
    // }
    // imwrite(output_directory + "\\finalStitched_blended.jpg", finalStitchedBlended);

    std::cout << "[testWarpPipeline] Complete — wrote all intermediate images.\n";
}

void saveMatBinary(const std::string &filename, const cv::Mat &mat)
{
    std::ofstream ofs(filename, std::ios::binary);
    int rows = mat.rows, cols = mat.cols, type = mat.type();
    ofs.write((char *)&rows, sizeof(rows));
    ofs.write((char *)&cols, sizeof(cols));
    ofs.write((char *)&type, sizeof(type));
    ofs.write((char *)mat.data, mat.total() * mat.elemSize());
}

cv::Mat loadMatBinary(const std::string &filename)
{
    std::ifstream ifs(filename, std::ios::binary);
    int rows, cols, type;
    ifs.read((char *)&rows, sizeof(rows));
    ifs.read((char *)&cols, sizeof(cols));
    ifs.read((char *)&type, sizeof(type));
    cv::Mat mat(rows, cols, type);
    ifs.read((char *)mat.data, mat.total() * mat.elemSize());
    return mat;
}

homographyMaps calculateHomographyMaps(
    const string &left_video_path,
    const string &right_video_path,
    const vector<double> &Kvec,
    const vector<double> &Dvec)
{
    homographyMaps homographyMapsResults;
    // --- Load first frames from videos ---
    VideoCapture capL(left_video_path);
    VideoCapture capR(right_video_path);
    int totalFramesL = (int)capL.get(cv::CAP_PROP_FRAME_COUNT);
    int totalFramesR = (int)capR.get(cv::CAP_PROP_FRAME_COUNT);

    cout << "Left video has " << totalFramesL << " frames" << endl;
    cout << "Right video has " << totalFramesR << " frames" << endl;

    int frameIndex = 0; // <-- pick the frame you want

    capL.set(cv::CAP_PROP_POS_FRAMES, frameIndex);
    capR.set(cv::CAP_PROP_POS_FRAMES, frameIndex);
    Mat frameL, frameR;
    // capL.read(frameL);
    // capR.read(frameR);
    if (!capL.read(frameL) || !capR.read(frameR))
    {
        cerr << "Error: Could not grab frame " << frameIndex << endl;
        return homographyMapsResults;
    }

    if (frameL.empty() || frameR.empty())
    {
        cerr << "Error: grabbed frame is empty" << endl;
        return homographyMapsResults;
    }
    capL.release();
    capR.release();

    imwrite("original_left.jpg", frameL);
    imwrite("original_right.jpg", frameR);

    int h = frameL.rows;
    int w = frameL.cols;

    // --- Calibration ---
    Mat K(3, 3, CV_64F, (void *)Kvec.data());
    K = K.clone(); // make a deep copy

    Mat D(1, (int)Dvec.size(), CV_64F, (void *)Dvec.data());
    D = D.clone();

    int bufferPixels = 500;

    pair<Mat, Mat> leftUndistortionMap = initUndistortCylindrialWarpMap(K, D, Mat::eye(3, 3, CV_64F), w, h, bufferPixels);
    pair<Mat, Mat> rightUndistortionMap = leftUndistortionMap;

    // --- Undistort ---
    Mat left, right;
    remap(frameL, left, leftUndistortionMap.first, leftUndistortionMap.second, INTER_AREA, BORDER_CONSTANT, cv::Scalar(0, 0, 0));
    remap(frameR, right, rightUndistortionMap.first, rightUndistortionMap.second, INTER_AREA, BORDER_CONSTANT, cv::Scalar(0, 0, 0));

    imwrite("undistorted_left.jpg", left);
    imwrite("undistorted_right.jpg", right);

    Mat leftMask = Mat::ones(left.size(), CV_8U);
    Mat rightMask = Mat::ones(right.size(), CV_8U);

    // --- Feature detection ---
    int detection_threshold = 60;
    Ptr<BRISK> detector = BRISK::create(detection_threshold);

    vector<KeyPoint> leftKeyPoints, rightKeyPoints;
    Mat leftDescriptors, rightDescriptors;
    detector->detectAndCompute(left, leftMask, leftKeyPoints, leftDescriptors);
    detector->detectAndCompute(right, rightMask, rightKeyPoints, rightDescriptors);

    cout << "feature threshold: " << detection_threshold
         << ", count: " << leftKeyPoints.size()
         << " " << rightKeyPoints.size() << endl;

    // --- Matching ---
    BFMatcher bf(NORM_HAMMING, true);
    vector<DMatch> matches;
    bf.match(leftDescriptors, rightDescriptors, matches);

    sort(matches.begin(), matches.end(),
         [](const DMatch &a, const DMatch &b)
         { return a.distance < b.distance; });

    vector<Point2f> leftPoints, rightPoints;
    for (auto &m : matches)
    {
        leftPoints.push_back(leftKeyPoints[m.queryIdx].pt);
        rightPoints.push_back(rightKeyPoints[m.trainIdx].pt);
    }

    cout << "feature matches: " << leftPoints.size() << endl;

    // --- Estimate affine ---
    Mat inliers;
    Mat affine = estimateAffine2D(rightPoints, leftPoints, inliers, RANSAC, 30);

    // Filter matches using inliers
    vector<Point2f> leftPointsInliers, rightPointsInliers;
    for (int i = 0; i < inliers.rows; i++)
    {
        if (inliers.at<uchar>(i))
        {
            leftPointsInliers.push_back(leftPoints[i]);
            rightPointsInliers.push_back(rightPoints[i]);
        }
    }

    tie(leftPointsInliers, rightPointsInliers) = selectPoints(
        leftPointsInliers, rightPointsInliers,
        left.cols, left.rows, 30);

    cout << "refined feature matches left: " << leftPointsInliers.size() << endl;
    cout << "refined feature matches right: " << rightPointsInliers.size() << endl;

    // --- Homography ---
    Mat homographyMatrix = findHomography(rightPointsInliers, leftPointsInliers);
    cout << "homography:\n"
         << homographyMatrix << endl;

    // --- Evaluate error ---
    vector<Point2f> projectedPoints;
    perspectiveTransform(rightPointsInliers, projectedPoints, homographyMatrix);

    double diff = 0.0;
    for (size_t i = 0; i < leftPointsInliers.size(); i++)
    {
        Point2f x = leftPointsInliers[i];
        Point2f z = projectedPoints[i];
        diff += sqrt((x.x - z.x) * (x.x - z.x) + (x.y - z.y) * (x.y - z.y));
    }
    cout << "matching diff: " << diff / leftPointsInliers.size() << endl;

    saveMatBinary("leftUndistortionMapX.bin", leftUndistortionMap.first);
    saveMatBinary("leftUndistortionMapY.bin", leftUndistortionMap.second);
    saveMatBinary("rightUndistortionMapX.bin", rightUndistortionMap.first);
    saveMatBinary("rightUndistortionMapY.bin", rightUndistortionMap.second);
    saveMatBinary("homographyMatrix.bin", homographyMatrix);
    return {leftUndistortionMap, rightUndistortionMap, homographyMatrix};
}

warpMaps calculateWarpMaps(
    const string &left_video_path,
    const string &right_video_path,
    const vector<double> &Kvec,
    const vector<double> &Dvec,
    int degrees = 0,
    int seam_x = 4800,
    int box_w = 7000,
    int box_h = 3000,
    int box_x = 1000,
    int box_y = 2400)
{
    warpMaps warpMapResults;
    // --- Load first frames from videos ---
    VideoCapture capL(left_video_path);
    VideoCapture capR(right_video_path);
    int totalFramesL = (int)capL.get(cv::CAP_PROP_FRAME_COUNT);
    int totalFramesR = (int)capR.get(cv::CAP_PROP_FRAME_COUNT);

    cout << "Left video has " << totalFramesL << " frames" << endl;
    cout << "Right video has " << totalFramesR << " frames" << endl;

    int frameIndex = 0; // <-- pick the frame you want

    capL.set(cv::CAP_PROP_POS_FRAMES, frameIndex);
    capR.set(cv::CAP_PROP_POS_FRAMES, frameIndex);
    Mat frameL, frameR;
    // capL.read(frameL);
    // capR.read(frameR);
    if (!capL.read(frameL) || !capR.read(frameR))
    {
        cerr << "Error: Could not grab frame " << frameIndex << endl;
        return warpMapResults;
    }

    if (frameL.empty() || frameR.empty())
    {
        cerr << "Error: grabbed frame is empty" << endl;
        return warpMapResults;
    }
    capL.release();
    capR.release();

    imwrite("original_left.jpg", frameL);
    imwrite("original_right.jpg", frameR);

    int h = frameL.rows;
    int w = frameL.cols;

    // --- Calibration ---
    Mat K(3, 3, CV_64F, (void *)Kvec.data());
    K = K.clone(); // make a deep copy

    Mat D(1, (int)Dvec.size(), CV_64F, (void *)Dvec.data());
    D = D.clone();

    int bufferPixels = 500;

    pair<Mat, Mat> leftUndistortionMap = initUndistortCylindrialWarpMap(K, D, Mat::eye(3, 3, CV_64F), w, h, bufferPixels);
    pair<Mat, Mat> rightUndistortionMap = leftUndistortionMap;

    // --- Undistort ---
    Mat left, right;
    remap(frameL, left, leftUndistortionMap.first, leftUndistortionMap.second, INTER_AREA, BORDER_CONSTANT, cv::Scalar(0, 0, 0));
    remap(frameR, right, rightUndistortionMap.first, rightUndistortionMap.second, INTER_AREA, BORDER_CONSTANT, cv::Scalar(0, 0, 0));

    imwrite("undistorted_left.jpg", left);
    imwrite("undistorted_right.jpg", right);

    Mat leftMask = Mat::ones(left.size(), CV_8U);
    Mat rightMask = Mat::ones(right.size(), CV_8U);

    // --- Feature detection ---
    int detection_threshold = 60;
    Ptr<BRISK> detector = BRISK::create(detection_threshold);

    vector<KeyPoint> leftKeyPoints, rightKeyPoints;
    Mat leftDescriptors, rightDescriptors;
    detector->detectAndCompute(left, leftMask, leftKeyPoints, leftDescriptors);
    detector->detectAndCompute(right, rightMask, rightKeyPoints, rightDescriptors);

    cout << "feature threshold: " << detection_threshold
         << ", count: " << leftKeyPoints.size()
         << " " << rightKeyPoints.size() << endl;

    // --- Matching ---
    BFMatcher bf(NORM_HAMMING, true);
    vector<DMatch> matches;
    bf.match(leftDescriptors, rightDescriptors, matches);

    sort(matches.begin(), matches.end(),
         [](const DMatch &a, const DMatch &b)
         { return a.distance < b.distance; });

    vector<Point2f> leftPoints, rightPoints;
    for (auto &m : matches)
    {
        leftPoints.push_back(leftKeyPoints[m.queryIdx].pt);
        rightPoints.push_back(rightKeyPoints[m.trainIdx].pt);
    }

    cout << "feature matches: " << leftPoints.size() << endl;

    // --- Estimate affine ---
    Mat inliers;
    Mat affine = estimateAffine2D(rightPoints, leftPoints, inliers, RANSAC, 30);

    // Filter matches using inliers
    vector<Point2f> leftPointsInliers, rightPointsInliers;
    for (int i = 0; i < inliers.rows; i++)
    {
        if (inliers.at<uchar>(i))
        {
            leftPointsInliers.push_back(leftPoints[i]);
            rightPointsInliers.push_back(rightPoints[i]);
        }
    }

    tie(leftPointsInliers, rightPointsInliers) = selectPoints(
        leftPointsInliers, rightPointsInliers,
        left.cols, left.rows, 30);

    cout << "refined feature matches left: " << leftPointsInliers.size() << endl;
    cout << "refined feature matches right: " << rightPointsInliers.size() << endl;

    // --- Homography ---
    Mat homographyMatrix = findHomography(rightPointsInliers, leftPointsInliers);
    cout << "homography:\n"
         << homographyMatrix << endl;

    // --- Evaluate error ---
    vector<Point2f> projectedPoints;
    perspectiveTransform(rightPointsInliers, projectedPoints, homographyMatrix);

    double diff = 0.0;
    for (size_t i = 0; i < leftPointsInliers.size(); i++)
    {
        Point2f x = leftPointsInliers[i];
        Point2f z = projectedPoints[i];
        diff += sqrt((x.x - z.x) * (x.x - z.x) + (x.y - z.y) * (x.y - z.y));
    }
    cout << "matching diff: " << diff / leftPointsInliers.size() << endl;

    // Rotation
    int rows = left.rows;
    int cols = left.cols;
    // get warped/rotated image on large canvas
    Point2f center(cols, rows);
    Mat affineMatrix = getRotationMatrix2D(center, degrees, 1.0); // 2x3
    Mat rotationMatrix = Mat::eye(3, 3, CV_64F);
    affineMatrix.copyTo(rotationMatrix(Rect(0, 0, 3, 2))); // place in top 2 rows

    // build complete/debug warp maps
    std::pair<std::pair<cv::Mat, cv::Mat>, std::pair<cv::Mat, cv::Mat>> warpMapsDebug = buildWarpMaps(rows, cols, homographyMatrix, rotationMatrix, 0, 0, cols * 2, rows * 2);
    std::pair<cv::Mat, cv::Mat> leftWarpMapDebug = warpMapsDebug.first;
    std::pair<cv::Mat, cv::Mat> rightWarpMapDebug = warpMapsDebug.second;
    std::pair<cv::Mat, cv::Mat> leftComboMapDebug =
        combineUndistortionAndWarp(leftWarpMapDebug.first, leftWarpMapDebug.second, leftUndistortionMap.first, leftUndistortionMap.second);
    std::pair<cv::Mat, cv::Mat> rightComboMapDebug =
        combineUndistortionAndWarp(rightWarpMapDebug.first, rightWarpMapDebug.second, rightUndistortionMap.first, rightUndistortionMap.second);

    std::pair<std::pair<cv::Mat, cv::Mat>, std::pair<cv::Mat, cv::Mat>> warpMaps = buildWarpMaps(rows, cols, homographyMatrix, rotationMatrix, box_x, box_y, box_w, box_h);
    std::pair<cv::Mat, cv::Mat> leftWarpMap = warpMaps.first;
    std::pair<cv::Mat, cv::Mat> rightWarpMap = warpMaps.second;
    std::pair<cv::Mat, cv::Mat> leftComboMap =
        combineUndistortionAndWarp(leftWarpMap.first, leftWarpMap.second, leftUndistortionMap.first, leftUndistortionMap.second);
    std::pair<cv::Mat, cv::Mat> rightComboMap =
        combineUndistortionAndWarp(rightWarpMap.first, rightWarpMap.second, rightUndistortionMap.first, rightUndistortionMap.second);

    saveMatBinary("leftComboMapDebugX.bin", leftComboMapDebug.first);
    saveMatBinary("leftComboMapDebugY.bin", leftComboMapDebug.second);
    saveMatBinary("rightComboMapDebugX.bin", rightComboMapDebug.first);
    saveMatBinary("rightComboMapDebugY.bin", rightComboMapDebug.second);
    saveMatBinary("leftComboMapX.bin", leftComboMap.first);
    saveMatBinary("leftComboMapY.bin", leftComboMap.second);
    saveMatBinary("rightComboMapX.bin", rightComboMap.first);
    saveMatBinary("rightComboMapY.bin", rightComboMap.second);
    return {leftComboMapDebug, rightComboMapDebug, leftComboMap, rightComboMap};
}

int main(int argc, char *argv[])
{
    if (argc < 2)
    {
        cerr << "Usage: StitchingApplication.exe config.json\n";
        return 1;
    }

    string configPath = argv[1];
    ifstream file(configPath);
    if (!file.is_open())
    {
        cerr << "Could not open config file: " << configPath << endl;
        return 1;
    }

    json cfg;
    file >> cfg;

    string mode = cfg.value("mode", "calculate");
    string leftVideoPath = cfg.value("left_video_path", "");
    string rightVideoPath = cfg.value("right_video_path", "");
    string output_directory = cfg.value("output_dir", "");
    double degrees = cfg.value("degrees", 0);
    int seam_x = cfg.value("seam_x", 4600);
    int box_x = cfg.value("box_x", 800);
    int box_y = cfg.value("box_y", 2500);
    int box_w = cfg.value("box_w", 6500);
    int box_h = cfg.value("box_h", 2500);
    std::vector<std::pair<int, int>> frameRanges;

    if (cfg.contains("frame_ranges") && cfg["frame_ranges"].is_array())
    {
        for (auto &range : cfg["frame_ranges"])
        {
            if (range.is_array() && range.size() == 2)
            {
                int start = range[0].get<int>();
                int end = range[1].get<int>();
                frameRanges.emplace_back(start, end);
            }
        }
    }

    cout << "[CONFIG LOADED]\n"
         << "  Mode: " << mode << "\n"
         << "  Left: " << leftVideoPath << "\n"
         << "  Right: " << rightVideoPath << "\n"
         << "  Degrees: " << degrees << "\n"
         << "  Box: (" << box_x << ", " << box_y
         << ", " << box_w << ", " << box_h << ")\n"
         << "  Frame Ranges: " << frameRanges.size() << " segments\n";
    for (const auto &[start, end] : frameRanges)
        cout << "    " << start << " → " << end << endl;

    // calibration values
    vector<double>
        Kvec9 = {2386.48907, 0.0, 2589.57600, 0.0, 2393.79274, 1468.20009, 0.0, 0.0, 1.0};
    vector<double> Dvec9 = {
        0.634646105,
        24.0696757,
        -0.000413849958,
        0.000454632934,
        3.84838450,
        0.930233556,
        24.3391322,
        10.4024500,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    };

    if (mode == "calculate")
    {

        cout << "Running calculateHomographyMaps..." << endl;
        homographyMaps homographyMapsResults = calculateHomographyMaps(leftVideoPath, rightVideoPath, Kvec9, Dvec9);

        testWarpPipeline2(output_directory, leftVideoPath, rightVideoPath, homographyMapsResults, degrees, seam_x, box_x, box_y, box_w, box_h);

        // cout << "Running calculateWarpMaps..." << endl;
        // warpMaps warpMapsResults = calculateWarpMaps(
        //     leftVideoPath, rightVideoPath,
        //     Kvec9, Dvec9,
        //     degrees,
        //     seam_x,
        //     box_w,
        //     box_h,
        //     box_x,
        //     box_y);
        // VideoCapture capL(leftVideoPath);
        // VideoCapture capR(rightVideoPath);
        // Mat distortedL, distortedR;
        // capL.read(distortedL);
        // capR.read(distortedR);
        // capL.release();
        // capR.release();

        // int rows = left.rows;
        // int cols = left.cols;
        // // get warped/rotated image on large canvas
        // Point2f center(cols, rows);
        // Mat affineMatrix = getRotationMatrix2D(center, degrees, 1.0); // 2x3
        // Mat rotationMatrix = Mat::eye(3, 3, CV_64F);
        // affineMatrix.copyTo(rotationMatrix(Rect(0, 0, 3, 2))); // place in top 2 rows

        // testWarpPipeline(output_directory, distortedL, distortedR, warpMapsResults.leftComboMapDebug, warpMapsResults.rightComboMapDebug, warpMapsResults.leftComboMap, warpMapsResults.rightComboMap, seam_x, box_x, box_y, box_w, box_h);
    }
    else if (mode == "debug")
    {
        homographyMaps homographyMapsResults;
        homographyMapsResults.leftUndistortionMap.first = loadMatBinary("leftUndistortionMapX.bin");
        homographyMapsResults.leftUndistortionMap.second = loadMatBinary("leftUndistortionMapY.bin");
        homographyMapsResults.rightUndistortionMap.first = loadMatBinary("rightUndistortionMapX.bin");
        homographyMapsResults.rightUndistortionMap.second = loadMatBinary("rightUndistortionMapY.bin");
        homographyMapsResults.homographyMatrix = loadMatBinary("homographyMatrix.bin");

        testWarpPipeline2(output_directory, leftVideoPath, rightVideoPath, homographyMapsResults, degrees, seam_x, box_x, box_y, box_w, box_h);

        // warpMaps warpMapsResults;
        // warpMapsResults.leftComboMapDebug.first = loadMatBinary("leftComboMapDebugX.bin");
        // warpMapsResults.leftComboMapDebug.second = loadMatBinary("leftComboMapDebugY.bin");
        // warpMapsResults.rightComboMapDebug.first = loadMatBinary("rightComboMapDebugX.bin");
        // warpMapsResults.rightComboMapDebug.second = loadMatBinary("rightComboMapDebugY.bin");
        // warpMapsResults.leftComboMap.first = loadMatBinary("leftComboMapX.bin");
        // warpMapsResults.leftComboMap.second = loadMatBinary("leftComboMapY.bin");
        // warpMapsResults.rightComboMap.first = loadMatBinary("rightComboMapX.bin");
        // warpMapsResults.rightComboMap.second = loadMatBinary("rightComboMapY.bin");

        // VideoCapture capL(leftVideoPath);
        // VideoCapture capR(rightVideoPath);
        // Mat distortedL, distortedR;
        // capL.set(cv::CAP_PROP_POS_FRAMES, 684);
        // capR.set(cv::CAP_PROP_POS_FRAMES, 684);
        // capL.read(distortedL);
        // capR.read(distortedR);
        // capL.release();
        // capR.release();

        // testWarpPipeline(output_directory, distortedL, distortedR, warpMapsResults.leftComboMapDebug, warpMapsResults.rightComboMapDebug, warpMapsResults.leftComboMap, warpMapsResults.rightComboMap, seam_x, box_x, box_y, box_w, box_h);
    }
    else if (mode == "stitch")
    {
        homographyMaps homographyMapsResults;
        homographyMapsResults.leftUndistortionMap.first = loadMatBinary("leftUndistortionMapX.bin");
        homographyMapsResults.leftUndistortionMap.second = loadMatBinary("leftUndistortionMapY.bin");
        homographyMapsResults.rightUndistortionMap.first = loadMatBinary("rightUndistortionMapX.bin");
        homographyMapsResults.rightUndistortionMap.second = loadMatBinary("rightUndistortionMapY.bin");
        homographyMapsResults.homographyMatrix = loadMatBinary("homographyMatrix.bin");

        // Rotation
        int undistortedRows = homographyMapsResults.leftUndistortionMap.first.rows;
        int undistortedCols = homographyMapsResults.leftUndistortionMap.first.cols;
        Point2f center(undistortedCols, undistortedRows);
        Mat affineMatrix = getRotationMatrix2D(center, degrees, 1.0); // 2x3
        Mat rotationMatrix = Mat::eye(3, 3, CV_64F);
        affineMatrix.copyTo(rotationMatrix(Rect(0, 0, 3, 2))); // place in top 2 rows

        std::pair<std::pair<cv::Mat, cv::Mat>, std::pair<cv::Mat, cv::Mat>> warpMaps = buildWarpMaps(undistortedRows, undistortedCols, homographyMapsResults.homographyMatrix, rotationMatrix, box_x, box_y, box_w, box_h);
        std::pair<cv::Mat, cv::Mat> leftWarpMap = warpMaps.first;
        std::pair<cv::Mat, cv::Mat> rightWarpMap = warpMaps.second;
        std::pair<cv::Mat, cv::Mat> leftComboMap =
            combineUndistortionAndWarp(leftWarpMap.first, leftWarpMap.second, homographyMapsResults.leftUndistortionMap.first, homographyMapsResults.leftUndistortionMap.second);
        std::pair<cv::Mat, cv::Mat> rightComboMap =
            combineUndistortionAndWarp(rightWarpMap.first, rightWarpMap.second, homographyMapsResults.rightUndistortionMap.first, homographyMapsResults.rightUndistortionMap.second);

        processAndStitchVideos(
            leftVideoPath,
            rightVideoPath,
            output_directory + "\\stitched_video.mp4",
            leftComboMap,
            rightComboMap,
            seam_x,
            box_w,
            box_h,
            box_x,
            box_y, frameRanges);
        // warpMaps warpMapsResults;
        // warpMapsResults.leftComboMap.first = loadMatBinary("leftComboMapX.bin");
        // warpMapsResults.leftComboMap.second = loadMatBinary("leftComboMapY.bin");
        // warpMapsResults.rightComboMap.first = loadMatBinary("rightComboMapX.bin");
        // warpMapsResults.rightComboMap.second = loadMatBinary("rightComboMapY.bin");
        // processAndStitchVideos(
        //     leftVideoPath,
        //     rightVideoPath,
        //     output_directory + "\\stitched_video.mp4",
        //     warpMapsResults.leftComboMap,
        //     warpMapsResults.rightComboMap,
        //     seam_x,
        //     box_w,
        //     box_h,
        //     box_x,
        //     box_y, frameRanges);
    }
    else
    {
        cerr << "Unknown mode: " << mode << endl;
        return 1;
    }
    return 0;
}
