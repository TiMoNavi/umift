#!/usr/bin/env swift

import AppKit
import Foundation
import PDFKit
import Vision

enum OCRError: Error, CustomStringConvertible {
    case usage
    case cannotOpenPDF(String)
    case cannotRenderPage(Int)

    var description: String {
        switch self {
        case .usage:
            return "usage: swift ocr_pdf_vision.swift <input.pdf> <output.md>"
        case .cannotOpenPDF(let path):
            return "cannot open PDF: \(path)"
        case .cannotRenderPage(let page):
            return "cannot render PDF page \(page)"
        }
    }
}

func render(page: PDFPage, scale: CGFloat = 3.0) throws -> CGImage {
    let bounds = page.bounds(for: .mediaBox)
    let width = max(1, Int(ceil(bounds.width * scale)))
    let height = max(1, Int(ceil(bounds.height * scale)))
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        throw OCRError.cannotRenderPage(page.pageRef?.pageNumber ?? 0)
    }

    context.setFillColor(NSColor.white.cgColor)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.saveGState()
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)
    context.restoreGState()

    guard let image = context.makeImage() else {
        throw OCRError.cannotRenderPage(page.pageRef?.pageNumber ?? 0)
    }
    return image
}

func recognize(image: CGImage) throws -> [String] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "en-US"]

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])

    return (request.results ?? [])
        .sorted { lhs, rhs in
            let verticalDelta = lhs.boundingBox.midY - rhs.boundingBox.midY
            if abs(verticalDelta) > 0.012 {
                return verticalDelta > 0
            }
            return lhs.boundingBox.minX < rhs.boundingBox.minX
        }
        .compactMap { $0.topCandidates(1).first?.string }
}

do {
    guard CommandLine.arguments.count == 3 else {
        throw OCRError.usage
    }

    let inputPath = CommandLine.arguments[1]
    let outputPath = CommandLine.arguments[2]
    guard let document = PDFDocument(url: URL(fileURLWithPath: inputPath)) else {
        throw OCRError.cannotOpenPDF(inputPath)
    }

    var markdown = "# TC20250717005 合同扫描件（任务书）OCR 文本\n\n"
    markdown += "> 来源：`\(URL(fileURLWithPath: inputPath).lastPathComponent)`。本文由 macOS Vision OCR 自动识别，版式、印章、手写内容及个别字符可能存在误差；涉及合同指标时应以原扫描件为准。\n\n"

    for index in 0..<document.pageCount {
        guard let page = document.page(at: index) else {
            throw OCRError.cannotRenderPage(index + 1)
        }
        fputs("OCR page \(index + 1)/\(document.pageCount)\n", stderr)
        let lines = try recognize(image: render(page: page))
        markdown += "## 第 \(index + 1) 页\n\n"
        markdown += lines.joined(separator: "\n\n")
        markdown += "\n\n"
    }

    try markdown.write(toFile: outputPath, atomically: true, encoding: .utf8)
} catch {
    fputs("error: \(error)\n", stderr)
    exit(1)
}
