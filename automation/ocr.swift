import Foundation
import Vision
import ImageIO
import CoreGraphics

// Analyze only a saved-image region at a larger raster scale. No screen capture
// or input occurs here and no expected SKU words are supplied to Vision.
func recognizeIdentityRegion(_ image: CGImage, box: [String: Any]) throws -> [[String: Any]] {
    guard let x = box["x"] as? Double, let y = box["y"] as? Double,
          let width = box["width"] as? Double, let height = box["height"] as? Double else { return [] }
    let left = max(0, x-0.008), top = max(0, y-0.004)
    let right = min(1, x+width+0.008), bottom = min(1, y+height+0.004)
    let rect = CGRect(x: left*Double(image.width), y: top*Double(image.height),
                      width: (right-left)*Double(image.width), height: (bottom-top)*Double(image.height)).integral
    guard let crop = image.cropping(to: rect),
          let context = CGContext(data: nil, width: crop.width*4, height: crop.height*4,
                                  bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return [] }
    context.interpolationQuality = .high
    context.draw(crop, in: CGRect(x: 0, y: 0, width: crop.width*4, height: crop.height*4))
    guard let enlarged = context.makeImage() else { return [] }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    let originalText = (box["text"] as? String) ?? ""
    let hasHan = originalText.range(of: #"\p{Han}"#, options: .regularExpression) != nil
    request.recognitionLanguages = hasHan ? ["zh-Hans", "en-US"] : ["en-US", "zh-Hans"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: enlarged, options: [:]).perform([request])
    return (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let b = observation.boundingBox
        return ["text": candidate.string, "confidence": candidate.confidence,
                "x": left+Double(b.minX)*(right-left), "y": top+Double(1-b.maxY)*(bottom-top),
                "width": Double(b.width)*(right-left), "height": Double(b.height)*(bottom-top)]
    }
}

// Local image analysis only. This process does not capture screens or control UI.
// Usage: mobile-ocr path [path ...], one JSON result per input image.
for filename in CommandLine.arguments.dropFirst() {
    do {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["zh-Hans", "en-US"]
        request.usesLanguageCorrection = false
        let handler = VNImageRequestHandler(url: URL(fileURLWithPath: filename), options: [:])
        try handler.perform([request])
        let lines: [[String: Any]] = (request.results ?? []).compactMap { observation in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            let b = observation.boundingBox
            return ["text": candidate.string, "confidence": candidate.confidence,
                    "x": Double(b.minX), "y": Double(1 - b.maxY), "width": Double(b.width), "height": Double(b.height)]
        }.sorted { ($0["y"] as! Double) < ($1["y"] as! Double) }
        // A second on-device recognition pass restricts Vision to the large
        // headline amount, excluding the neighbouring weekly-change badge.
        // Its vertical position is anchored to the actually recognized metric
        // label; no date or amount is inferred from chart coordinates.
        let anchors = lines.filter {
            (($0["text"] as? String) ?? "").filter { !$0.isWhitespace }.contains("近7日成交均价")
        }
        var priceLines: [[String: Any]] = []
        var priceROI: [String: Double]? = nil
        var priceRegions: [[String: Any]] = []
        if anchors.count == 1, let anchorY = anchors[0]["y"] as? Double,
           anchorY > 0.1, anchorY < 0.75 {
            let top = anchorY + 0.025
            let bottom = min(anchorY + 0.085, 0.98)
            let left = 0.025
            var right = 0.335
            // When the full-image pass already isolates the complete headline
            // currency, preserve its entire measured width (including five-digit
            // amounts) and avoid cutting into the adjacent badge. A merged line
            // has no reliable character crop boundary and keeps the conservative
            // default; Python then requires exact full-prefix corroboration and
            // rejects any amount touching the crop boundary.
            let wholeCurrency = #"^[¥￥Y]\s*\d[\d,]*(?:\.\d{1,2})?$"#
            var prefixRight: Double? = nil
            var isolated = false
            for observation in request.results ?? [] {
                guard let candidate = observation.topCandidates(1).first else { continue }
                let b = observation.boundingBox
                let y = Double(1-b.maxY)
                if y > anchorY, y < anchorY+0.095, b.minX < 0.2 {
                    if candidate.string.range(of: wholeCurrency, options: .regularExpression) != nil {
                        right = min(0.70, max(0.12, Double(b.maxX)+0.008))
                        isolated = true
                    }
                    if let range = candidate.string.range(of: #"^[¥￥Y]\s*\d[\d,]*(?:\.\d{1,2})?"#, options: .regularExpression),
                       let prefix = try candidate.boundingBox(for: range) {
                        // Vision's word-level box is only a localization hint.
                        // Every crop still needs independent full-currency OCR,
                        // exact whole-image amount agreement and edge clearance.
                        prefixRight = min(0.70, max(0.12, Double(prefix.boundingBox.maxX)+0.008))
                    }
                }
            }
            var rights = [right]
            if let prefix = prefixRight, !rights.contains(where: { abs($0-prefix) < 0.001 }) { rights.append(prefix) }
            if !isolated {
                for padding in [0.003, 0.005] {
                    let edge = min(0.70, right+padding)
                    if !rights.contains(where: { abs($0-edge) < 0.001 }) { rights.append(edge) }
                }
            }
            if !isolated, !rights.contains(where: { abs($0-0.35) < 0.001 }) { rights.append(0.35) }
            for boundary in rights {
                let priceRequest = VNRecognizeTextRequest()
                priceRequest.recognitionLevel = .accurate
                priceRequest.recognitionLanguages = ["zh-Hans", "en-US"]
                priceRequest.usesLanguageCorrection = false
                priceRequest.regionOfInterest = CGRect(x: left, y: 1-bottom, width: boundary-left, height: bottom-top)
                try handler.perform([priceRequest])
                let regionLines: [[String: Any]] = (priceRequest.results ?? []).compactMap { observation in
                    guard let candidate = observation.topCandidates(1).first else { return nil }
                    let b = observation.boundingBox
                    return ["text": candidate.string, "confidence": candidate.confidence,
                            "x": left + Double(b.minX) * (boundary-left),
                            "y": top + Double(1-b.maxY) * (bottom-top),
                            "width": Double(b.width) * (boundary-left), "height": Double(b.height) * (bottom-top)]
                }
                let roi = ["left": left, "top": top, "right": boundary, "bottom": bottom]
                priceRegions.append(["lines": regionLines, "roi": roi])
                if priceRegions.count == 1 { priceLines = regionLines; priceROI = roi }
            }
        }
        var output: [String: Any] = ["path": filename, "lines": lines,
                                   "price_lines": priceLines, "price_regions": priceRegions, "price_parser": "anchor-roi-v2"]
        if let roi = priceROI { output["price_roi"] = roi }
        if let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: filename) as CFURL, nil),
           let image = CGImageSourceCreateImageAtIndex(source, 0, nil) {
            let queryBox = lines.first {
                let y = $0["y"] as! Double, x = $0["x"] as! Double
                return y >= 0.105 && y < 0.155 && x >= 0.08 && x < 0.8
            }
            let modelBox = lines.first {
                let y = $0["y"] as! Double, x = $0["x"] as! Double, text = $0["text"] as! String
                return y > 0.18 && y < 0.24 && x > 0.25 && x < 0.75 &&
                    text.range(of: #"[A-Za-z0-9]"#, options: .regularExpression) != nil &&
                    text.range(of: #"人在|选择|CORE|搜索|回收|折|%"#, options: [.regularExpression, .caseInsensitive]) == nil
            }
            if let box = queryBox { output["query_lines"] = try recognizeIdentityRegion(image, box: box) }
            if let box = modelBox { output["model_lines"] = try recognizeIdentityRegion(image, box: box) }
            output["identity_parser"] = "saved-region-upscale-language-aware-v2"
        }
        let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    } catch {
        let output = ["path": filename, "error": "Local OCR failed: \(error.localizedDescription)"]
        let data = try! JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    }
}
