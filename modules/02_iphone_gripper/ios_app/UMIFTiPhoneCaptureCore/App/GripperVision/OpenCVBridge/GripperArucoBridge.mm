#include <memory>
#include <vector>
#include <cmath>
#include <algorithm>
#include <limits>

#import <opencv2/core.hpp>
#import <opencv2/imgproc.hpp>
#import <opencv2/calib3d.hpp>
#import <opencv2/objdetect/aruco_detector.hpp>

#import "GripperArucoBridge.h"

namespace {
constexpr int kLeftMarkerID = 0;
constexpr int kRightMarkerID = 1;
constexpr double kMarkerSizeMeters = 0.016;
constexpr double kFullFrame35mmDiagonal = 43.266615305567875;
constexpr double kUltraWideEquivalentFocalLengthMM = 14.0;
constexpr double kUltraWideActualFocalLengthMM = 2.22;
constexpr double kRejectedCandidateMinArea = 80.0;
constexpr double kRightCandidateMinInteriorAngle = 35.0;
constexpr double kRightCandidateMaxInteriorAngle = 145.0;
constexpr double kRightCandidateMinLargestInteriorAngle = 110.0;
constexpr double kRightCandidateMaxLargestInteriorAngle = 145.0;
constexpr double kLeftCandidateMinLargestInteriorAngle = 86.0;
constexpr double kLeftCandidateMaxLargestInteriorAngle = 132.0;

cv::Mat makeCameraMatrix(int width, int height) {
    const double sensorDiagonalMM =
        kUltraWideActualFocalLengthMM * kFullFrame35mmDiagonal / kUltraWideEquivalentFocalLengthMM;
    const double sensorWidthMM = sensorDiagonalMM * 4.0 / 5.0;
    const double sensorHeightMM = sensorDiagonalMM * 3.0 / 5.0;
    const double fx = kUltraWideActualFocalLengthMM / sensorWidthMM * static_cast<double>(width);
    const double fy = kUltraWideActualFocalLengthMM / sensorHeightMM * static_cast<double>(height);
    return (cv::Mat_<double>(3, 3) << fx, 0.0, width / 2.0,
                                      0.0, fy, height / 2.0,
                                      0.0, 0.0, 1.0);
}

std::vector<cv::Point3f> makeMarkerObjectPoints() {
    const float half = static_cast<float>(kMarkerSizeMeters / 2.0);
    return {
        cv::Point3f(-half,  half, 0.0f),
        cv::Point3f( half,  half, 0.0f),
        cv::Point3f( half, -half, 0.0f),
        cv::Point3f(-half, -half, 0.0f),
    };
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

double clampDouble(double value, double low, double high) {
    return std::max(low, std::min(high, value));
}

double smoothStep01(double value) {
    const double t = clampDouble(value, 0.0, 1.0);
    return t * t * (3.0 - 2.0 * t);
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

struct QuadShapeLimits {
    double minArea;
    double maxSideRatio;
    double minCompactness;
    double minFillRatio;
    double minInteriorAngle;
    double maxInteriorAngle;
    double minLargestInteriorAngle;
    double maxLargestInteriorAngle;
};

QuadShapeLimits shapeLimitsForCandidate(const std::vector<cv::Point2f> &points, int frameWidth) {
    double centerX = 0.0;
    for (const cv::Point2f &point : points) {
        centerX += static_cast<double>(point.x);
    }
    centerX /= static_cast<double>(std::max<size_t>(1, points.size()));

    const double xNorm = frameWidth > 0
        ? clampDouble(centerX / static_cast<double>(frameWidth), 0.0, 1.0)
        : 0.5;

    if (xNorm >= 0.5) {
        const double edgeFactor = smoothStep01((xNorm - 0.5) / 0.45);
        return {
            kRejectedCandidateMinArea,
            3.3 + 1.7 * edgeFactor,
            0.026 - 0.006 * edgeFactor,
            0.20 - 0.05 * edgeFactor,
            kRightCandidateMinInteriorAngle,
            kRightCandidateMaxInteriorAngle,
            kRightCandidateMinLargestInteriorAngle,
            kRightCandidateMaxLargestInteriorAngle,
        };
    }

    const double leftEdgeFactor = smoothStep01((0.5 - xNorm) / 0.5);
    return {
        kRejectedCandidateMinArea,
        3.1 + 0.6 * leftEdgeFactor,
        0.028 - 0.004 * leftEdgeFactor,
        0.22 - 0.04 * leftEdgeFactor,
        50.0 - 8.0 * leftEdgeFactor,
        124.0 + 6.0 * leftEdgeFactor,
        kLeftCandidateMinLargestInteriorAngle,
        kLeftCandidateMaxLargestInteriorAngle,
    };
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

bool isPlausibleRejectedMarkerQuad(const std::vector<cv::Point2f> &points, int frameWidth) {
    if (points.size() != 4 || !isConvexOrderedQuad(points)) {
        return false;
    }

    const QuadShapeLimits limits = shapeLimitsForCandidate(points, frameWidth);
    const double area = polygonArea(points);
    if (area < limits.minArea) {
        return false;
    }

    double perimeter = 0.0;
    double minSide = std::numeric_limits<double>::max();
    double maxSide = 0.0;
    for (size_t i = 0; i < points.size(); ++i) {
        const double side = pointDistance(points[i], points[(i + 1) % points.size()]);
        minSide = std::min(minSide, side);
        maxSide = std::max(maxSide, side);
        perimeter += side;
    }
    if (minSide < 10.0 || maxSide / minSide > limits.maxSideRatio) {
        return false;
    }

    const double compactness = area / (perimeter * perimeter);
    if (compactness < limits.minCompactness) {
        return false;
    }

    const cv::Rect bounds = cv::boundingRect(points);
    if (bounds.width < 16 || bounds.height < 16) {
        return false;
    }
    const double fillRatio = area / static_cast<double>(bounds.width * bounds.height);
    if (fillRatio < limits.minFillRatio) {
        return false;
    }

    double largestAngle = 0.0;
    for (size_t i = 0; i < points.size(); ++i) {
        const double angle = interiorAngleDegrees(points, i);
        if (angle < limits.minInteriorAngle || angle > limits.maxInteriorAngle) {
            return false;
        }
        largestAngle = std::max(largestAngle, angle);
    }
    if (largestAngle < limits.minLargestInteriorAngle || largestAngle > limits.maxLargestInteriorAngle) {
        return false;
    }

    return true;
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
    for (size_t i = 0; i < points.size(); ++i) {
        [cornerValues addObject:@{
            @"x": @(points[i].x),
            @"y": @(points[i].y),
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

cv::Mat detectionGrayImageFromPixelBuffer(CVPixelBufferRef pixelBuffer, int roiTopY, NSString **mode) {
    const OSType pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer);
    const int width = static_cast<int>(CVPixelBufferGetWidth(pixelBuffer));
    const int height = static_cast<int>(CVPixelBufferGetHeight(pixelBuffer));
    if (width <= 0 || height <= 0 || roiTopY < 0 || roiTopY >= height) {
        return cv::Mat();
    }
    const int roiHeight = height - roiTopY;

    if (pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange ||
        pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange) {
        *mode = @"y-plane";
        const size_t rowBytes = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0);
        void *baseAddress = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0);
        if (baseAddress == nullptr) {
            return cv::Mat();
        }
        cv::Mat yPlane(height, width, CV_8UC1, baseAddress, rowBytes);
        return yPlane(cv::Rect(0, roiTopY, width, roiHeight));
    }

    *mode = @"bgra";
    const size_t rowBytes = CVPixelBufferGetBytesPerRow(pixelBuffer);
    void *baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer);
    if (baseAddress == nullptr) {
        return cv::Mat();
    }
    cv::Mat bgra(height, width, CV_8UC4, baseAddress, rowBytes);
    cv::Mat gray;
    cv::cvtColor(bgra(cv::Rect(0, roiTopY, width, roiHeight)), gray, cv::COLOR_BGRA2GRAY);
    return gray;
}
}  // namespace

@implementation GripperArucoBridge {
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
        _detectorParameters.adaptiveThreshWinSizeMax = 53;
        _detectorParameters.adaptiveThreshWinSizeStep = 4;
        _detectorParameters.minMarkerPerimeterRate = 0.015;
        _detectorParameters.maxMarkerPerimeterRate = 4.0;
        _detectorParameters.polygonalApproxAccuracyRate = 0.06;
        _detectorParameters.minCornerDistanceRate = 0.03;
        _detectorParameters.minDistanceToBorder = 0;
        _detectorParameters.minOtsuStdDev = 3.0;
        _detectorParameters.errorCorrectionRate = 0.8;
        _detectorParameters.maxErroneousBitsInBorderRate = 0.45;
        _detector = std::make_unique<cv::aruco::ArucoDetector>(_dictionary, _detectorParameters);
    }
    return self;
}

- (NSDictionary<NSString *, id> *)analyzePixelBuffer:(CVPixelBufferRef)pixelBuffer {
    CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    @try {
        const int width = static_cast<int>(CVPixelBufferGetWidth(pixelBuffer));
        const int height = static_cast<int>(CVPixelBufferGetHeight(pixelBuffer));
        const int roiTopY = height * 5 / 6;
        NSString *inputMode = @"unknown";
        cv::Mat detectionGray = detectionGrayImageFromPixelBuffer(pixelBuffer, roiTopY, &inputMode);
        if (detectionGray.empty()) {
            return @{
                @"frameWidth": @(width),
                @"frameHeight": @(height),
                @"inputMode": inputMode,
                @"detectedIDs": @[],
                @"markers": @[],
                @"rejectedCandidates": @[],
                @"tagCount": @0,
                @"rejectedCount": @0,
                @"widthRawM": [NSNull null],
            };
        }

        std::vector<std::vector<cv::Point2f>> corners;
        std::vector<int> ids;
        std::vector<std::vector<cv::Point2f>> rejected;
        _detector->detectMarkers(detectionGray, corners, ids, rejected);
        offsetPoints(corners, 0.0f, static_cast<float>(roiTopY));
        offsetPoints(rejected, 0.0f, static_cast<float>(roiTopY));

        NSMutableArray<NSNumber *> *detectedIDs = [NSMutableArray array];
        NSMutableArray<NSDictionary *> *markers = [NSMutableArray array];
        NSMutableArray<NSDictionary *> *rejectedCandidates = [NSMutableArray array];
        NSMutableDictionary<NSNumber *, NSDictionary *> *poseByID = [NSMutableDictionary dictionary];
        NSMutableDictionary<NSNumber *, NSDictionary *> *centerByID = [NSMutableDictionary dictionary];

        const cv::Mat cameraMatrix = makeCameraMatrix(width, height);
        const cv::Mat distCoeffs = cv::Mat::zeros(4, 1, CV_64F);
        const std::vector<cv::Point3f> objectPoints = makeMarkerObjectPoints();

        for (size_t index = 0; index < ids.size(); ++index) {
            const int markerID = ids[index];
            [detectedIDs addObject:@(markerID)];
            const cv::Point2f markerCenter = diagonalIntersectionOrMean(corners[index]);
            const double markerCenterX = markerCenter.x;
            const double markerCenterY = markerCenter.y;
            [markers addObject:candidateDictionary(markerID, corners[index])];

            if (markerID == kLeftMarkerID || markerID == kRightMarkerID) {
                centerByID[@(markerID)] = @{
                    @"x": @(markerCenterX),
                    @"y": @(markerCenterY),
                };
            } else {
                continue;
            }

            cv::Mat imagePoints(4, 1, CV_32FC2);
            for (int i = 0; i < 4; ++i) {
                imagePoints.at<cv::Vec2f>(i, 0) = cv::Vec2f(corners[index][i].x, corners[index][i].y);
            }
            cv::Mat objectPointsMat(4, 1, CV_32FC3);
            for (int i = 0; i < 4; ++i) {
                objectPointsMat.at<cv::Vec3f>(i, 0) = cv::Vec3f(objectPoints[i].x, objectPoints[i].y, objectPoints[i].z);
            }

            cv::Mat rvec;
            cv::Mat tvec;
            const bool ok = cv::solvePnP(
                objectPointsMat,
                imagePoints,
                cameraMatrix,
                distCoeffs,
                rvec,
                tvec,
                false,
                cv::SOLVEPNP_IPPE_SQUARE
            );
            if (!ok || tvec.rows < 3) {
                continue;
            }

            const double x = tvec.at<double>(0, 0);
            const double y = tvec.at<double>(1, 0);
            const double z = tvec.at<double>(2, 0);
            poseByID[@(markerID)] = @{
                @"x": @(x),
                @"y": @(y),
                @"z": @(z),
            };
        }

        for (const auto &candidate : rejected) {
            if (isPlausibleRejectedMarkerQuad(candidate, width)) {
                [rejectedCandidates addObject:candidateDictionary(-1, candidate)];
            }
        }

        NSNumber *leftX = poseByID[@(kLeftMarkerID)][@"x"];
        NSNumber *rightX = poseByID[@(kRightMarkerID)][@"x"];
        NSNumber *leftZ = poseByID[@(kLeftMarkerID)][@"z"];
        NSNumber *rightZ = poseByID[@(kRightMarkerID)][@"z"];
        NSNumber *leftCenterX = centerByID[@(kLeftMarkerID)][@"x"];
        NSNumber *leftCenterY = centerByID[@(kLeftMarkerID)][@"y"];
        NSNumber *rightCenterX = centerByID[@(kRightMarkerID)][@"x"];
        NSNumber *rightCenterY = centerByID[@(kRightMarkerID)][@"y"];

        id widthRawM = [NSNull null];
        if (leftX != nil && rightX != nil) {
            widthRawM = @(rightX.doubleValue - leftX.doubleValue);
        } else if (leftX != nil) {
            widthRawM = @(fabs(leftX.doubleValue) * 2.0);
        } else if (rightX != nil) {
            widthRawM = @(fabs(rightX.doubleValue) * 2.0);
        }

        NSMutableArray<NSNumber *> *zValues = [NSMutableArray array];
        if (leftZ != nil) {
            [zValues addObject:leftZ];
        }
        if (rightZ != nil) {
            [zValues addObject:rightZ];
        }

        id meanZ = [NSNull null];
        if (zValues.count > 0) {
            double total = 0.0;
            for (NSNumber *value in zValues) {
                total += value.doubleValue;
            }
            meanZ = @(total / static_cast<double>(zValues.count));
        }

        id pixelDistance = [NSNull null];
        if (leftCenterX != nil && leftCenterY != nil && rightCenterX != nil && rightCenterY != nil) {
            const double dx = rightCenterX.doubleValue - leftCenterX.doubleValue;
            const double dy = rightCenterY.doubleValue - leftCenterY.doubleValue;
            pixelDistance = @(std::sqrt(dx * dx + dy * dy));
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
            @"widthRawM": widthRawM,
            @"pixelDistance": pixelDistance,
            @"meanZ": meanZ,
            @"leftX": leftX ?: [NSNull null],
            @"rightX": rightX ?: [NSNull null],
            @"leftZ": leftZ ?: [NSNull null],
            @"rightZ": rightZ ?: [NSNull null],
            @"leftCenterX": leftCenterX ?: [NSNull null],
            @"leftCenterY": leftCenterY ?: [NSNull null],
            @"rightCenterX": rightCenterX ?: [NSNull null],
            @"rightCenterY": rightCenterY ?: [NSNull null],
        };
    } @finally {
        CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    }
}

@end
