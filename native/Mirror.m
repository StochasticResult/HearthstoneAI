#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <objc/runtime.h>

@interface HearthMirrorStatus : NSObject
- (id)initWithPID:(int)pid withBlocking:(BOOL)blocking;
- (id)getSceneMgrState;
- (id)getGameType;
- (id)getFormat;
- (id)getSelectedDeck;
- (id)getDecks;
- (id)getMulliganState;
- (id)getBoardState:(BOOL)includeOpposing;
- (id)getBigCardState;
- (BOOL)isPopupShowing;
- (BOOL)isGameMenuVisible;
- (BOOL)isMulliganWaitingForUserInput;
- (BOOL)isShopOpen;
@end

// Limit serialization to public, relevant model classes: never print game
// server info, credentials, account IDs, or unrelated process data.
static id jsonValue(id value, int depth) {
    if (!value || value == [NSNull null]) return [NSNull null];
    if (depth > 8) return @"[depth limit]";
    if ([value isKindOfClass:[NSString class]] || [value isKindOfClass:[NSNumber class]]) return value;
    if ([value isKindOfClass:[NSArray class]]) {
        NSMutableArray *a = [NSMutableArray array];
        for (id v in value) [a addObject:jsonValue(v, depth + 1)];
        return a;
    }
    if ([value isKindOfClass:[NSDictionary class]]) {
        NSMutableDictionary *d = [NSMutableDictionary dictionary];
        for (id key in value) d[[key description]] = jsonValue(value[key], depth + 1);
        return d;
    }
    NSString *className = NSStringFromClass([value class]);
    NSSet *allowed = [NSSet setWithArray:@[@"MirrorSceneMgrState", @"MirrorDeck", @"MirrorCard",
        @"MirrorBoardState", @"MirrorPlayZoneState", @"MirrorBoardCard",
        @"MirrorMulliganState", @"MirrorMulliganCardState", @"MirrorBigCardState"]];
    if (![allowed containsObject:className]) return [NSString stringWithFormat:@"[%@]", className];
    NSMutableDictionary *out = [NSMutableDictionary dictionary];
    unsigned count = 0;
    objc_property_t *props = class_copyPropertyList([value class], &count);
    for (unsigned i = 0; i < count; i++) {
        NSString *name = [NSString stringWithUTF8String:property_getName(props[i])];
        @try { out[name] = jsonValue([value valueForKey:name], depth + 1); }
        @catch (NSException *e) { out[name] = @"[unavailable]"; }
    }
    free(props);
    return out;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        BOOL deckLookup = argc == 3 && strncmp(argv[2], "--deck=", 7) == 0;
        NSNumber *requestedDeck = nil;
        if (deckLookup) {
            const char *number = argv[2] + 7;
            char *end = NULL;
            unsigned long long deckId = strtoull(number, &end, 10);
            if (!*number || strlen(number) > 19 || !deckId || *end || *number < '0' || *number > '9') return 2;
            requestedDeck = @(deckId);
        }
        if (argc < 2 || argc > 3 || (argc == 3 && !deckLookup && strcmp(argv[2], "--board") != 0 && strcmp(argv[2], "--hover") != 0)) {
            fprintf(stderr, "Usage: game_status PATH_TO_HEARTHMIRROR [--board|--hover|--deck=ID]\n"); return 2;
        }
        NSArray *apps = [NSRunningApplication runningApplicationsWithBundleIdentifier:@"unity.Blizzard Entertainment.Hearthstone"];
        if (apps.count != 1) { fprintf(stderr, "Expected one running Hearthstone instance.\n"); return 1; }
        void *framework = dlopen(argv[1], RTLD_NOW);
        if (!framework) { fprintf(stderr, "%s\n", dlerror()); return 1; }
        int (*acquire)(void) = dlsym(framework, "acquireTaskportRight");
        if (!acquire || acquire() != 0) { fprintf(stderr, "Cannot acquire process access.\n"); return 1; }
        Class cls = objc_getClass("HearthMirror");
        if (!cls) { fprintf(stderr, "HearthMirror class is unavailable.\n"); return 1; }
        HearthMirrorStatus *h = [[cls alloc] initWithPID:[apps.firstObject processIdentifier] withBlocking:YES];
        if (!h) { fprintf(stderr, "HearthMirror initialization failed.\n"); return 1; }
        NSMutableDictionary *out = [NSMutableDictionary dictionary];
        out[@"scene"] = jsonValue([h getSceneMgrState], 0);
        out[@"game_type"] = jsonValue([h getGameType], 0);
        out[@"format"] = jsonValue([h getFormat], 0);
        id selected = [h getSelectedDeck];
        out[@"selected_deck"] = jsonValue(selected, 0);
        // getSelectedDeck is an ID, not a MirrorDeck. Only menu polling needs
        // the collection lookup; normal board reads must remain lightweight.
        id lookupId = requestedDeck ?: selected;
        if ((argc == 2 || deckLookup) && [lookupId isKindOfClass:[NSNumber class]]) {
            id decks = [h getDecks];
            if ([decks isKindOfClass:[NSArray class]] && [decks count] <= 1000) {
                for (id deck in decks) {
                    if (![NSStringFromClass([deck class]) isEqualToString:@"MirrorDeck"]) continue;
                    id deckId = [deck valueForKey:@"id"];
                    if ([deckId isEqual:lookupId]) {
                        out[deckLookup ? @"deck_details" : @"selected_deck_details"] = @{@"id": deckId,
                            @"cards": jsonValue([deck valueForKey:@"cards"], 0)};
                        break;
                    }
                }
            }
        }
        out[@"popup"] = @([h isPopupShowing]);
        out[@"game_menu"] = @([h isGameMenuVisible]);
        out[@"shop_open"] = @([h isShopOpen]);
        out[@"mulligan_waiting"] = @([h isMulliganWaitingForUserInput]);
        if (argc == 3 && strcmp(argv[2], "--board") == 0) { // Normal menu polling avoids board reads.
            out[@"mulligan"] = jsonValue([h getMulliganState], 0);
            out[@"board"] = jsonValue([h getBoardState:YES], 0);
        }
        if (argc == 3 && strcmp(argv[2], "--hover") == 0) {
            out[@"hover"] = jsonValue([h getBigCardState], 0);
        }
        NSError *error = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:out options:0 error:&error];
        if (!json) { fprintf(stderr, "%s\n", error.localizedDescription.UTF8String); return 1; }
        fwrite(json.bytes, 1, json.length, stdout); fputc('\n', stdout);
        return 0;
    }
}
