import sys

USAGE = "usage: python -m git_gate call <tool>   (arguments as one JSON object on stdin)"

if sys.argv[1:2] == ["call"]:
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    from git_gate.call import main

    main(sys.argv[2])
elif len(sys.argv) >= 2 and sys.argv[1] == "handover":
    from git_gate.handover import main

    sys.exit(main(sys.argv[2:]))
elif len(sys.argv) >= 2 and sys.argv[1] == "rules":
    from git_gate.rulescli import main

    sys.exit(main(sys.argv[2:]))
else:
    from git_gate.server import main

    main()
