#include <algorithm>
#include <cmath>
#include <limits>
#include <memory>
#include <vector>

#import <opencv2/core.hpp>
#import <opencv2/imgproc.hpp>
#import <opencv2/objdetect/aruco_detector.hpp>

#import "ARUWGripperArucoBridge.h"

namespace {
constexpr double kMinRejectedArea = 42.0;
constexpr int kMaxRejectedCandidates = 96;

double clampDouble(double value, double low, double high) {
    return std::max(low, std::min(high, value));
}

double polygonArea(const std::vector<cv::Point2f> &points) {
    if (points.size() < 3) {
        return 0.0;
    }
    double area = 0.0;
    for (size_t i = 0; i < points.size(); ++i) {
        const cv::Point2f &a = points[i];
        const cv::Point2f &b = points[(i + 1) % points.size()];
        area += static_cast<double>(a.x) * static_cast<double>(b.y) -
                static_cast<double>(b.x) * static_cast<double>(a.y);
    }
    return std::fabs(area) * 0.5;
}

double pointDistance(const cv::Point2f &a, const cv::Point2f &b) {
    const double dx = static_cast<double>(a.x) - static_cast<double>(b.x);
    const double dy = static_cast<double>(a.y) - static_cast<double>(b.y);
    return std::sqrt(dx * dx + dy * dy);
}

bool isConvexOrderedQuad(const std::vector<cv::Point2f> &points) {
    if (points.size() != 4) {
        return false;
    }
    int sign = 0;
    for (size_t i = 0; i < points.size(); ++i) {
        const cv::Point2f a = points[i];
        const cv::Point2f b = points[(i + 1) % points.size()];
        const cv::Point2f c = points[(i + 2) % points.size()];
        const cv::Point2f ab = b - a;
        const cv::Point2f bc = c - b;
        const double cross = static_cast<double>(ab.x) * static_cast<double>(bc.y) -
                             static_cast<double>(ab.y) * static_cast<double>(bc.x);
        if (std::fabs(cross) < 1e-3) {
            return false;
        }
        const int nextSign = cross > 0.0 ? 1 : -1;
        if (sign == 0) {
            sign = nextSign;
        } else if (sign != nextSign) {
            return false;
        }
    }
    return true;
}

double interiorAngleDegrees(const std::vector<cv::Point2f> &points, size_t index) {
    const cv::Point2f prev = points[(index + points.size() - 1) % points.size()] - points[index];
    const cv::Point2f next = points[(index + 1) % points.size()] - points[index];
    const double prevLength = std::sqrt(static_cast<double>(prev.x) * prev.x + static_cast<double>(prev.y) * prev.y);
    const double nextLength = std::sqrt(static_cast<double>(next.x) * next.x + static_cast<double>(next.y) * next.y);
    if (prevLength < 1e-6 || nextLength < 1e-6) {
        return 0.0;
    }
    const double dot = static_cast<double>(prev.x) * static_cast<double>(next.x) +
                       static_cast<double>(prev.y) * static_cast<double>(next.y);
    const double cosine = clampDouble(dot / (prevLength * nextLength), -1.0, 1.0);
    return std::acos(cosine) * 180.0 / CV_PI;
}

bool isPlausibleDebugQuad(const std::vector<cv::Point2f> &points, int width, int height) {
    if (points.size() != 4 || !isConvexOrderedQuad(points)) {
        return false;
    }

    const double area = polygonArea(points);
    if (area < kMinRejectedArea) {
        return false;
    }

    const cv::Rect bounds = cv::boundingRect(points);
    if (bounds.width < 7 || bounds.height < 7) {
        return false;
    }
    if (bounds.x < -2 || bounds.y < -2 || bounds.x + bounds.width > width + 2 || bounds.y + bounds.height > height + 2) {
        return false;
    }

    double minSide = std::numeric_limits<double>::max();
    double maxSide = 0.0;
    double perimeter = 0.0;
    for (size_t i = 0; i < points.size(); ++i) {
        const double side = pointDistance(points[i], points[(i + 1) % points.size()]);
        minSide = std::min(minSide, side);
        maxSide = std::max(maxSide, side);
        perimeter += side;
    }
    if (minSide < 5.0 || maxSide / minSide > 7.5) {
        return false;
    }
    if (area / (perimeter * perimeter) < 0.010) {
        return false;
    }

    double largestAngle = 0.0;
    for (size_t i = 0; i < points.size(); ++i) {
        const double angle = interiorAngleDegrees(points, i);
        if (angle < 18.0 || angle > 162.0) {
            return false;
        }
        largestAngle = std::max(largestAngle, angle);
    }
    return largestAngle <= 154.0;
}

cv::Point2f diagonalIntersectionOrMean(const std::vector<cv::Point2f> &points) {
    if (points.size() != 4) {
        return cv::Point2f(0.0f, 0.0f);
    }

    const cv::Point2f p0 = points[0];
    const cv::Point2f p1 = points[1];
    const cv::Point2f p2 = points[2];
    const cv::Point2f p3 = points[3];
    const cv::Point2f r = p2 - p0;
    const cv::Point2f s = p3 - p1;
    const double denominator = static_cast<double>(r.x) * static_cast<double>(s.y) -
                               static_cast<double>(r.y) * static_cast<double>(s.x);
    if (std::fabs(denominator) > 1e-6) {
        const cv::Point2f delta = p1 - p0;
        const double t = (static_cast<double>(delta.x) * static_cast<double>(s.y) -
                          static_cast<double>(delta.y) * static_cast<double>(s.x)) / denominator;
        return p0 + static_cast<float>(t) * r;
    }

    cv::Point2f mean(0.0f, 0.0f);
    for (const cv::Point2f &point : points) {
        mean += point;
    }
    return mean * 0.25f;
}

NSDictionary *candidateDictionary(int markerID, const std::vector<cv::Point2f> &points) {
    NSMutableArray<NSDictionary *> *cornerValues = [NSMutableArray arrayWithCapacity:4];
    for (const cv::Point2f &point : points) {
        [cornerValues addObject:@{
            @"x": @(point.x),
            @"y": @(point.y),
        }];
    }
    const cv::Point2f center = diagonalIntersectionOrMean(points);
    return @{
        @"id": @(markerID),
        @"centerX": @(center.x),
        @"centerY": @(center.y),
        @"area": @(polygonArea(points)),
        @"corners": cornerValues,
    };
}

void offsetPoints(std::vector<std::vector<cv::Point2f>> &pointSets, float dx, float dy) {
    for (auto &points : pointSets) {
        for (auto &point : points) {
            point.x += dx;
            point.y += dy;
        }
    }
}

cv::Mat grayImageFromPixelBuffer(CVPixelBufferRef pixelBuffer, NSString **mode) {
    const OSType pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer);
    const int width = static_cast<int>(CVPixelBufferGetWidth(pixelBuffer));
    const int height = static_cast<int>(CVPixelBufferGetHeight(pixelBuffer));

    if (pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange ||
        pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange) {
        *mode = @"y-plane";
        const size_t rowBytes = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0);
        void *baseAddress = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0);
        if (baseAddress == nullptr) {
            return cv::Mat();
        }
        return cv::Mat(height, width, CV_8UC1, baseAddress, rowBytes);
    }

    *mode = @"bgra";
    const size_t rowBytes = CVPixelBufferGetBytesPerRow(pixelBuffer);
    void *baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer);
    if (baseAddress == nullptr) {
        return cv::Mat();
    }
    cv::Mat bgra(height, width, CV_8UC4, baseAddress, rowBytes);
    cv::Mat gray;
    cv::cvtColor(bgra, gray, cv::COLOR_BGRA2GRAY);
    return gray;
}
}  // namespace

@implementation ARUWGripperArucoBridge {
    cv::aruco::Dictionary _dictionary;
    cv::aruco::DetectorParameters _detectorParameters;
    std::unique_ptr<cv::aruco::ArucoDetector> _detector;
}

- (instancetype)init {
    self = [super init];
    if (self) {
        _dictionary = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_4X4_50);
        _detectorParameters.cornerRefinementMethod = cv::aruco::CORNER_REFINE_SUBPIX;
        _detectorParameters.adaptiveThreshWinSizeMin = 3;
        _detectorParameters.adaptiveThreshWinSizeMax = 41;
        _detectorParameters.adaptiveThreshWinSizeStep = 4;
        _detectorParameters.minMarkerPerimeterRate = 0.018;
        _detectorParameters.maxMarkerPerimeterRate = 3.0;
        _detectorParameters.polygonalApproxAccuracyRate = 0.055;
        _detectorParameters.minCornerDistanceRate = 0.025;
        _detectorParameters.minDistanceToBorder = 0;
        _detectorParameters.minOtsuStdDev = 2.6;
        _detectorParameters.errorCorrectionRate = 0.7;
        _detectorParameters.maxErroneousBitsInBorderRate = 0.42;
        _detector = std::make_unique<cv::aruco::ArucoDetector>(_dictionary, _detectorParameters);
    }
    return self;
}

- (NSDictionary<NSString *, id> *)analyzePixelBuffer:(CVPixelBufferRef)pixelBuffer {
    CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    @try {
        const int width = static_cast<int>(CVPixelBufferGetWidth(pixelBuffer));
        const int height = static_cast<int>(CVPixelBufferGetHeight(pixelBuffer));
        NSString *inputMode = @"unknown";
        cv::Mat gray = grayImageFromPixelBuffer(pixelBuffer, &inputMode);
        if (gray.empty()) {
            return @{
                @"frameWidth": @(width),
                @"frameHeight": @(height),
                @"inputMode": inputMode,
                @"detectedIDs": @[],
                @"markers": @[],
                @"rejectedCandidates": @[],
                @"tagCount": @0,
                @"rejectedCount": @0,
            };
        }

        const int roiTopY = std::max(0, height * 11 / 20);
        const int roiHeight = height - roiTopY;
        cv::Mat detectionGray = gray(cv::Rect(0, roiTopY, width, roiHeight));

        std::vector<std::vector<cv::Point2f>> corners;
        std::vector<int> ids;
        std::vector<std::vector<cv::Point2f>> rejected;
        _detector->detectMarkers(detectionGray, corners, ids, rejected);
        offsetPoints(corners, 0.0f, static_cast<float>(roiTopY));
        offsetPoints(rejected, 0.0f, static_cast<float>(roiTopY));

        NSMutableArray<NSNumber *> *detectedIDs = [NSMutableArray array];
        NSMutableArray<NSDictionary *> *markers = [NSMutableArray array];
        NSMutableArray<NSDictionary *> *rejectedCandidates = [NSMutableArray array];

        for (size_t index = 0; index < ids.size(); ++index) {
            [detectedIDs addObject:@(ids[index])];
            [markers addObject:candidateDictionary(ids[index], corners[index])];
        }

        std::vector<std::vector<cv::Point2f>> plausibleRejected;
        plausibleRejected.reserve(rejected.size());
        for (const auto &candidate : rejected) {
            if (isPlausibleDebugQuad(candidate, width, height)) {
                plausibleRejected.push_back(candidate);
            }
        }
        std::sort(plausibleRejected.begin(), plausibleRejected.end(), [](const auto &lhs, const auto &rhs) {
            return polygonArea(lhs) > polygonArea(rhs);
        });
        const size_t rejectedLimit = std::min(plausibleRejected.size(), static_cast<size_t>(kMaxRejectedCandidates));
        for (size_t index = 0; index < rejectedLimit; ++index) {
            [rejectedCandidates addObject:candidateDictionary(-1, plausibleRejected[index])];
        }

        return @{
            @"frameWidth": @(width),
            @"frameHeight": @(height),
            @"inputMode": inputMode,
            @"roiTopY": @(roiTopY),
            @"detectedIDs": detectedIDs,
            @"markers": markers,
            @"rejectedCandidates": rejectedCandidates,
            @"tagCount": @(detectedIDs.count),
            @"rejectedCount": @(rejected.size()),
            @"keptRejectedCount": @(rejectedCandidates.count),
        };
    } @finally {
        CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    }
}

@end
